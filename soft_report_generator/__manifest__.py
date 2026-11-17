# -*- coding: utf-8 -*-
{
    'name': 'Soft Report Generator - Word Template Engine',
    'version': '19.0.1.0.2',
    'category': 'Productivity',
    'summary': 'Word (.docx) templates -> PDF via LibreOffice; '
               'placeholder mapping, table loops, conditions, HTML fields',
    'description': """
Generic Word template engine for Odoo 19
========================================

Upload a .docx, map its ``{{placeholders}}`` to fields of any Odoo model and
print the result as docx, PDF (converted by LibreOffice), HTML, ODT or RTF.

This is the model-agnostic core. Model-specific helpers live in add-on
modules (e.g. ``soft_report_generator_sale``).
model technical names (``soft.report.template`` and friends) are unchanged.
""",
    'author': 'Softteknik Bilişim',
    'website': 'https://softteknik.com',
    'license': 'AGPL-3',
    'depends': ['base', 'mail'],
    'external_dependencies': {
        'bin': ['soffice'],
        # Odoo 19 turns a missing ``bin`` entry into an install/upgrade error
        # that names the apt package when the map below has one:
        #   Unable to install module "soft_report_generator" because an
        #   external dependency is not met: Unable to find 'soffice' in path
        #   It can be installed running: apt install libreoffice-writer
        'apt': {'soffice': 'libreoffice-writer'},
    },
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'wizard/report_template_wizard_views.xml',
        'views/report_template_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
