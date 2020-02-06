# -*- coding: utf-8 -*-
# Copyright 2020 Akretion France (http://www.akretion.com/)
# @author: Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from dateutil.relativedelta import relativedelta
from datetime import datetime
from odoo.addons.l10n_fr_siret.models.partner import _check_luhn
from unidecode import unidecode
import base64
import logging

logger = logging.getLogger(__name__)

try:
    from unidecode import unidecode
except ImportError:
    unidecode = False
    logger.debug('Cannot import unidecode')


FRANCE_CODES = ('FR', 'GP', 'MQ', 'GF', 'RE', 'YT', 'NC',
                'PF', 'TF', 'MF', 'BL', 'PM', 'WF')
AMOUNT_FIELDS = [
    'fee_amount', 'commission_amount', 'brokerage_amount',
    'discount_amount', 'attendance_fee_amount', 'copyright_royalties_amount',
    'licence_royalties_amount', 'other_income_amount', 'allowance_amount',
    'benefits_in_kind_amount', 'withholding_tax_amount']


class L10nFrDas2(models.Model):
    _name = 'l10n.fr.das2'
    _inherit = ['mail.thread']
    _order = 'year desc'
    _description = 'DAS2'

    year = fields.Integer(
        string='Year', required=True, states={'done': [('readonly', True)]},
        track_visibility='onchange',
        default=lambda self: self._default_year())
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
        ], default='draft', readonly=True, string='State')
    company_id = fields.Many2one(
        'res.company', string='Company',
        ondelete='cascade', required=True,
        states={'done': [('readonly', True)]},
        default=lambda self: self.env['res.company']._company_default_get())
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True, store=True,
        string='Company Currency')
    payment_journal_ids = fields.Many2many(
        'account.journal',
        string='Payment Journals', required=True,
        default=lambda self: self._default_payment_journals(),
        states={'done': [('readonly', True)]})
    line_ids = fields.One2many(
        'l10n.fr.das2.line', 'parent_id', string='Lines',
        states={'done': [('readonly', True)]})
    partner_declare_threshold = fields.Integer(
        string='Partner Declaration Threshold', readonly=True)
    # option for draft moves ?
    contact_id = fields.Many2one(
        'res.partner', string='Administrative Contact',
        states={'done': [('readonly', True)]},
        default=lambda self: self.env.user.partner_id.id,
        help='Contact in the company for the fiscal administration: the name, email and phone number of this partner will be used in the file.')
    attachment_id = fields.Many2one(
        'ir.attachment', string='File Export', readonly=True)

    _sql_constraints = [(
        'year_company_uniq',
        'unique(company_id, year)',
        'A DAS2 already exists for that year!')]

    @api.model
    def _default_payment_journals(self):
        res = []
        pay_journals = self.env['account.journal'].search([
            ('type', 'in', ('bank', 'cash')),
            ('company_id', '=', self.env.user.company_id.id)])
        if pay_journals:
            res = pay_journals.ids
        return res

    @api.model
    def _default_year(self):
        today = datetime.today()
        prev_year = today - relativedelta(years=1)
        return prev_year.year

    @api.depends('year')
    def name_get(self):
        res = []
        for rec in self:
            res.append((rec.id, 'DAS2 %s' % rec.year))
        return res

    def done(self):
        self.state = 'done'
        return

    def back2draft(self):
        self.state = 'draft'
        return

    def unlink(self):
        for rec in self:
            if rec.state == 'done':
                raise UserError(_(
                    "Cannot delete declaration %s in done state.")
                    % rec.display_name)
        return super(L10nFrDas2, self).unlink()

    def generate_lines(self):
        self.ensure_one()
        amlo = self.env['account.move.line']
        lfdlo = self.env['l10n.fr.das2.line']
        company = self.company_id
        if not company.country_id:
            raise UserError(_(
                "Country not set on company '%s'.") % company.display_name)
        if company.country_id.code not in FRANCE_CODES:
            raise UserError(_(
                "Company '%s' is configured in country '%s'. The DAS2 is "
                "only for France and it's oversea territories.")
                % (company.display_name,
                   company.country_id.name))
        if company.currency_id != self.env.ref('base.EUR'):
            raise UserError(_(
                "Company '%s' is configured with currency '%s'. "
                "It should be EUR.")
                % (company.display_name,
                   company.currency_id.name))
        if company.fr_das2_partner_declare_threshold <= 0:
            raise UserError(_(
                "The DAS2 partner declaration threshold is not set on "
                "company '%s'.") % company.display_name)
        self.partner_declare_threshold =\
            company.fr_das2_partner_declare_threshold
        das2_partners = self.env['res.partner'].search([
            ('parent_id', '=', False),
            ('fr_das2_type', '!=', False)])
        self.line_ids.unlink()
        if not das2_partners:
            raise UserError(_(
                "There are no partners configured for DAS2."))
        base_domain = [
            ('company_id', '=', self.company_id.id),
            ('date', '>=', '%d-01-01' % self.year),
            ('date', '<=', '%d-12-31' % self.year),
            ('journal_id', 'in', self.payment_journal_ids.ids),
            ('balance', '!=', 0),
            ]
        for partner in das2_partners:
            mlines = amlo.search(base_domain + [
                ('partner_id', '=', partner.id),
                ('account_id', '=', partner.property_account_payable_id.id),
                ])
            note = []
            amount = 0.0
            for mline in mlines:
                amount += mline.balance
                if mline.full_reconcile_id:
                    rec = _('reconciliation mark %s') % mline.full_reconcile_id.name
                else:
                    rec = _('not reconciled')
                note.append(_(
                    "Payment dated %s in journal '%s': %.2f € (%s, journal entry %s)") % (
                    mline.date,
                    mline.journal_id.display_name,
                    mline.balance,
                    rec,
                    mline.move_id.name))
            if note:
                field_name = '%s_amount' % partner.fr_das2_type
                vals = {
                    field_name: int(round(amount, 2)),
                    'parent_id': self.id,
                    'partner_id': partner.id,
                    'job': partner.fr_das2_job,
                    'note': '\n'.join(note),
                    }
                if partner.siren and partner.nic:
                    vals['partner_siret'] = partner.siret
                lfdlo.create(vals)
        self.add_warning_in_chatter(das2_partners)

    def add_warning_in_chatter(self, das2_partners):
        amlo = self.env['account.move.line']
        aao = self.env['account.account']
        ajo = self.env['account.journal']
        rpo = self.env['res.partner']
        company = self.company_id
        # The code below is just to write warnings in chatter
        purchase_journals = ajo.search([
            ('type', '=', 'purchase'),
            ('company_id', '=', company.id)])
        das2_accounts = aao.search([
            ('company_id', '=', company.id),
            '|', '|', '|', '|',
            ('code', '=like', '6222%'),
            ('code', '=like', '6226%'),
            ('code', '=like', '6228%'),
            ('code', '=like', '653%'),
            ('code', '=like', '6516%'),
            ])
        rg_res = amlo.read_group([
            ('company_id', '=', company.id),
            ('date', '>=', '%d-01-01' % self.year),
            ('date', '<=', '%d-12-31' % self.year),
            ('journal_id', 'in', purchase_journals.ids),
            ('partner_id', '!=', False),
            ('partner_id', 'not in', das2_partners.ids),
            ('account_id', 'in', das2_accounts.ids),
            ('balance', '!=', 0),
            ], ['partner_id'], ['partner_id'])
        if rg_res:
            msg = _(
                "<p>The following partners are not configured for DAS2 but "
                "they have expenses in some accounts that indicate "
                "that maybe they should be configured for DAS2:</p><ul>")
            for rg_re in rg_res:
                partner = rpo.browse(rg_re['partner_id'][0])
                msg += '<li><a href=# data-oe-model=res.partner data-oe-id=%d>%s</a>' % (
                    partner.id,
                    partner.display_name)
            msg += '</ul>'
            self.message_post(msg)

    @api.model
    def _prepare_field(self, field_name, partner, value, size, required=False, numeric=False):
        '''This function is designed to be inherited.'''
        if numeric:
            if not value:
                value = 0
            if not isinstance(value, int):
                try:
                    value = int(value)
                except Exception:
                    raise UserError(_(
                        "Failed to convert field '%s' (partner %s) to integer.")
                        % (field_name, partner.display_name))
            value = str(value)
            if len(value) > size:
                raise UserError(_(
                    "Field %s (partner %s) has value %s: "
                    "it is bigger than the maximum size "
                    "(%d characters)")
                    % (field_name, partner.display_name, value, size))
            if len(value) < size:
                value = value.rjust(size, '0')
            return value
        if required and not value:
            raise UserError(_(
                "The field '%s' (partner %s) is empty or 0. It should have a non-null "                    "value.") % (field_name, partner.display_name))
        if not value:
            value = ' ' * size
        # Cut if too long
        value = value[0:size]
        # enlarge if too small
        if len(value) < size:
            value = value.ljust(size, ' ')
        assert len(value) == size, 'The length of the field is wrong'
        return value

    def _prepare_address(self, partner):
        cstreet2 = self._prepare_field('Street2', partner, partner.street2, 32)
        cstreet = self._prepare_field('Street', partner, partner.street, 32)
        # specs section 5.4 : only bureau distributeur and code postal are
        # required. And they say it is tolerated to set city as
        # bureau distributeur
        # And we don't set the commune field because we put the same info
        # in bureau distributeur (section 5.4.1.3)
        # specs section 5.4 and 5.4.1.2.b: it is possible to set the field
        # "Adresse voie" without structuration on 32 chars => that's what we do
        if not partner.city:
            raise UserError(_(
                "Missing city on partner '%s'.") % partner.display_name)

        if partner.country_id and partner.country_id.code not in FRANCE_CODES:
            if not partner.country_id.fr_cog:
                raise UserError(_(
                    u"Missing Code Officiel Géographique on country '%s'.")
                    % partner.country_id.display_name)
            cog = self._prepare_field(
                'COG', partner, partner.country_id.fr_cog, 5, True, numeric=True)
            raw_country_name = partner.with_context(lang='fr_FR').country_id.name
            country_name = self._prepare_field(
                'Nom du pays', partner, raw_country_name, 26, True)
            raw_commune = partner.city
            if partner.zip:
                raw_commune = '%s %s' % (partner.zip, partner.city)
            commune = self._prepare_field('Commune', partner, raw_commune, 26, True)
            caddress = cstreet2 + ' ' + cstreet + cog + ' ' + commune + cog + ' ' + country_name
        # According to the specs, we should have some code specific for DOM-TOM
        # But it's not easy, because we are supposed to give the INSEE code
        # of the city, not of the territory => we don't handle that for the moment
        else:
            ccity = self._prepare_field('City', partner, partner.city, 26, True)
            czip = self._prepare_field('Zip', partner, partner.zip, 5, True)
            caddress = cstreet2 + ' ' + cstreet + '0' * 5 + ' ' + ' ' * 26 + czip + ' ' + ccity
        assert len(caddress) == 129
        return caddress

    def generate_file(self):
        self.ensure_one()
        company = self.company_id
        cpartner = company.partner_id
        if not self.line_ids:
            raise UserError(_("The DAS2 has no lines."))
        if not company.siret:
            raise UserError(_(
                "Missing SIRET on company '%s'.") % company.display_name)
        if not company.ape:
            raise UserError(_(
                "Missing APE on company '%s'.") % company.display_name)
        if not company.street:
            raise UserError(_(
                "Missing Street on company '%s'") % company.display_name)
        contact = self.contact_id
        if not contact:
            raise UserError(_("Missing administrative contact."))
        if not contact.email:
            raise UserError(_(
                "The email is not set on the administrative contact "
                "partner '%s'.") % contact.display_name)
        if not contact.phone and not contact.mobile:
            raise UserError(_(
                "The phone number is not set on the administrative contact "
                "partner '%s'.") % contact.display_name)
        if self.attachment_id:
            raise UserError(_(
                "An export file already exists. First, delete it via the "
                "attachments and then re-generate it."))

        csiren = self._prepare_field('SIREN', cpartner, cpartner.siren, 9, True)
        csiret = self._prepare_field('SIRET', cpartner, cpartner.siret, 14, True)
        cape = self._prepare_field('APE', cpartner, company.ape, 5, True)
        cname = self._prepare_field('Name', cpartner, company.name, 50, True)
        file_type = 'X'  # tous déclarants honoraires seuls
        year = str(self.year)
        assert len(year) == 4
        caddress = self._prepare_address(cpartner)
        cprefix = csiret + '01' + year + '5'
        # line 010 Company header
        flines = []
        flines.append(
            csiren + '0' * 12 + '010' + ' ' * 14 + cape + ' ' * 4 +
            cname + caddress + ' ' * 8 + file_type + csiret + ' ' * 5 +
            caddress + ' ' + ' ' * 288)

        # ligne 020 Etablissement header
        flines.append(
            cprefix + '020' + ' ' * 14 + cape + '0' * 14 + ' ' * 41 +
            cname + caddress + ' ' * 40 + ' ' * 53 + 'N' * 6 + ' ' * 296)

        for line in self.line_ids:
            partner = line.partner_id
            if (
                    partner.country_id and
                    partner.country_id.code in FRANCE_CODES and
                    not line.partner_siret):
                raise UserError(_(
                    "Missing SIRET for french partner %s.")
                    % partner.display_name)
            # ligne 210 honoraire
            if partner.is_company:
                partner_name = self._prepare_field('Partner name', partner, partner.name, 50, True)
                lastname = ' ' * 30
                firstname = ' ' * 20
            else:
                partner_name = ' ' * 50
                if hasattr(partner, 'firstname') and partner.firstname:
                    lastname = self._prepare_field('Partner lastname', partner, partner.lastname, 30, True)
                    firstname = self._prepare_field('Partner firstname', partner, partner.firstname, 20, True)
                else:
                    lastname = self._prepare_field('Partner name', partner, partner.name, 30, True)
                    firstname = ' ' * 20
            address = self._prepare_address(partner)
            partner_siret = self._prepare_field('SIRET', partner, line.partner_siret, 14)
            job = self._prepare_field('Profession', partner, line.job, 30)
            amount_fields_list = [
                self._prepare_field(x, partner, line[x], 10, numeric=True)
                for x in AMOUNT_FIELDS]
            if line.benefits_in_kind_amount:
                bik_letters = ''
                bik_letters += line.benefits_in_kind_food and 'N' or ' '
                bik_letters += line.benefits_in_kind_accomodation and 'L' or ' '
                bik_letters += line.benefits_in_kind_car and 'V' or ' '
                bik_letters += line.benefits_in_kind_other and 'A' or ' '
                bik_letters += line.benefits_in_kind_nict and 'T' or ' '
            else:
                bik_letters = ' ' * 5
            if line.allowance_amount:
                allow_letters = ''
                allow_letters += line.allowance_fixed and 'F' or ' '
                allow_letters += line.allowance_real and 'R' or ' '
                allow_letters += line.allowance_employer and 'P' or ' '
            else:
                allow_letters = ' ' * 3
            flines.append(
                cprefix + '210' + partner_siret + partner_name + firstname +
                lastname + job + address + ''.join(amount_fields_list) +
                bik_letters + allow_letters +
                ' ' * 2 + '0' * 10 + ' ' * 245)
        rg = self.env['l10n.fr.das2.line'].read_group([('parent_id', '=', self.id)], AMOUNT_FIELDS, [])[0]
        total_fields_list = [
            self._prepare_field(x, cpartner, rg[x], 12, numeric=True)
            for x in AMOUNT_FIELDS]
        contact_name = self._prepare_field('Administrative contact name', contact, contact.name, 50)
        contact_email = self._prepare_field('Administrative contact email', contact, contact.email, 60)
        phone = contact.phone or contact.mobile
        phone = phone.replace(' ', '').replace('.', '').replace('-', '')
        if phone.startswith('+33'):
            phone = '0%s' % phone[3:]
        contact_phone = self._prepare_field('Administrative contact phone', contact, phone, 10)
        flines.append(
            cprefix + '300' + ' ' * 36 + '0' * 12 * 9 +
            ''.join(total_fields_list) + ' ' * 12 + '0' * 12 * 2 + '0' * 6 +
            '0' * 12 * 5 + ' ' * 74 +
            contact_name + contact_phone + contact_email + ' ' * 76)

        lines_number = self._prepare_field('Number of lines', cpartner, len(self.line_ids), 6, numeric=True)
        flines.append(
            csiren + '9' * 12 + '310' + '00001' + '0' * 6 + lines_number +
            '0' * 6 * 3 + ' ' * 18 + '9' * 12 * 9 +
            ''.join(total_fields_list) + ' ' * 12 + '0' * 12 * 2 + '0' * 6 +
            '0' * 12 * 5 + ' ' * 253)
        for fline in flines:
            if len(fline) != 672:
                raise UserError(_(
                    "One of the lines has a length of %d. "
                    "All lines should have a length of 672. Line: %s.")
                    % (len(fline), fline))
        file_content = '\n'.join(flines)
        file_content_encoded = file_content.encode('latin1')
        filename = 'DAS2_%s.txt' % self.year
        attach = self.env['ir.attachment'].create({
            'name': filename,
            'res_id': self.id,
            'res_model': self._name,
            'datas': base64.encodestring(file_content_encoded),
            'datas_fname': filename,
            })
        self.attachment_id = attach.id
        action = {
            'type': 'ir.actions.act_window',
            'name': _('DAS2 Export File'),
            'view_mode': 'form',
            'res_model': 'ir.attachment',
            'target': 'current',
            'res_id': attach.id,
            }
        try:
            action['view_id'] = self.env.ref(
                'account_payment_order.view_attachment_simplified_form').id
        except Exception:
            pass
        return action

    def button_lines_fullscreen(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window'].for_xml_id(
            'l10n_fr_das2', 'l10n_fr_das2_line_action')
        action.update({
            'domain': [('parent_id', '=', self.id)],
            'views': False,
            })
        return action


class L10nFrDas2Line(models.Model):
    _name = 'l10n.fr.das2.line'
    _description = 'DAS2 line'

    parent_id = fields.Many2one(
        'l10n.fr.das2', string='DAS2 Report', ondelete='cascade')
    partner_id = fields.Many2one(
        'res.partner', string='Supplier', ondelete='restrict',
        domain=[('parent_id', '=', False)],
        states={'done': [('readonly', True)]}, required=True)
    partner_siret = fields.Char(
        string='Partner SIRET', size=14, states={'done': [('readonly', True)]})
    company_id = fields.Many2one(
        related='parent_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='parent_id.company_id.currency_id', store=True, readonly=True,
        string='Company Currency')
    fee_amount = fields.Integer(string=u'Honoraires et vacations', states={'done': [('readonly', True)]})
    commission_amount = fields.Integer(string=u'Commissions', states={'done': [('readonly', True)]})
    brokerage_amount = fields.Integer(string=u'Courtages', states={'done': [('readonly', True)]})
    discount_amount = fields.Integer(string=u'Ristournes', states={'done': [('readonly', True)]})
    attendance_fee_amount = fields.Integer(string=u'Jetons de présence', states={'done': [('readonly', True)]})
    copyright_royalties_amount = fields.Integer(string=u"Droits d'auteur", states={'done': [('readonly', True)]})
    licence_royalties_amount = fields.Integer(string=u"Droits d'inventeur", states={'done': [('readonly', True)]})
    other_income_amount = fields.Integer(string=u'Autre rémunérations', states={'done': [('readonly', True)]})
    allowance_amount = fields.Integer(string=u'Indemnités et remboursements', states={'done': [('readonly', True)]})
    benefits_in_kind_amount = fields.Integer(string='Avantages en nature', states={'done': [('readonly', True)]})
    withholding_tax_amount = fields.Integer(string=u'Retenue à la source', states={'done': [('readonly', True)]})
    total_amount = fields.Integer(
        compute='_compute_total_amount', string='Total Amount', store=True, readonly=True)
    to_declare = fields.Boolean(
        compute='_compute_total_amount', string='To Declare', readonly=True)
    allowance_fixed = fields.Boolean(u'Allocation forfaitaire', states={'done': [('readonly', True)]})
    allowance_real = fields.Boolean(u'Sur frais réels', states={'done': [('readonly', True)]})
    allowance_employer = fields.Boolean(u"Prise en charge directe par l'employeur", states={'done': [('readonly', True)]})
    benefits_in_kind_food = fields.Boolean(u'Nourriture', states={'done': [('readonly', True)]})
    benefits_in_kind_accomodation = fields.Boolean(u'Logement', states={'done': [('readonly', True)]})
    benefits_in_kind_car = fields.Boolean(u'Voiture', states={'done': [('readonly', True)]})
    benefits_in_kind_other = fields.Boolean(u'Autres', states={'done': [('readonly', True)]})
    benefits_in_kind_nict = fields.Boolean(u'Outils issus des NTIC', states={'done': [('readonly', True)]})
    state = fields.Selection(
        related='parent_id.state', store=True, readonly=True)
    note = fields.Text()
    job = fields.Char(string='Profession', size=30)

    _sql_constraints = [
        ('fee_amount_positive', 'CHECK(fee_amount >= 0)', 'Negative amounts not allowed!'),
        ('commission_amount_positive', 'CHECK(commission_amount >= 0)', 'Negative amounts not allowed!'),
        ('brokerage_amount_positive', 'CHECK(brokerage_amount >= 0)', 'Negative amounts not allowed!'),
        ('discount_amount_positive', 'CHECK(discount_amount >= 0)', 'Negative amounts not allowed!'),
        ('attendance_fee_amount_positive', 'CHECK(attendance_fee_amount >= 0)', 'Negative amounts not allowed!'),
        ('copyright_royalties_amount_positive', 'CHECK(copyright_royalties_amount >= 0)', 'Negative amounts not allowed!'),
        ('licence_royalties_amount_positive', 'CHECK(licence_royalties_amount >= 0)', 'Negative amounts not allowed!'),
        ('other_income_amount_positive', 'CHECK(other_income_amount >= 0)', 'Negative amounts not allowed!'),
        ('allowance_amount_positive', 'CHECK(allowance_amount >= 0)', 'Negative amounts not allowed!'),
        ('benefits_in_kind_amount_positive', 'CHECK(benefits_in_kind_amount >= 0)', 'Negative amounts not allowed!'),
        ('withholding_tax_amount_positive', 'CHECK(withholding_tax_amount >= 0)', 'Negative amounts not allowed!'),
        ]

    @api.depends(
        'parent_id.partner_declare_threshold',
        'fee_amount', 'commission_amount',
        'brokerage_amount', 'discount_amount',
        'attendance_fee_amount', 'copyright_royalties_amount',
        'licence_royalties_amount', 'other_income_amount',
        'allowance_amount', 'benefits_in_kind_amount',
        'withholding_tax_amount')
    def _compute_total_amount(self):
        for line in self:
            amount_total = 0
            for field_name in AMOUNT_FIELDS:
                amount_total += line[field_name]
            to_declare = False
            if line.parent_id:
                if amount_total >= line.parent_id.partner_declare_threshold:
                    to_declare = True
            line.to_declare = to_declare
            line.amount_total = amount_total

    @api.constrains('partner_siret')
    def check_siret(self):
        for line in self:
            if line.partner_siret:
                if len(line.partner_siret) != 14:
                    raise ValidationError(_(
                        "SIRET %s is invalid: it must have 14 digits.")
                        % line.partner_siret)
                if not _check_luhn(line.partner_siret):
                    raise ValidationError(_(
                        "SIRET %s is invalid: the checksum is wrong.")
                        % line.partner_siret)

    @api.onchange('partner_id')
    def partner_id_change(self):
        if self.partner_id and self.partner_id.siren and self.partner_id.nic:
            self.partner_siret = self.partner_id.siret