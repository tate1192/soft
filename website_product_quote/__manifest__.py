# -*- coding: utf-8 -*-
{
    'name': "Website Product Quote",
    'summary': """ subtitle on modules listing or apps.openerp.com  """,
    'author': 'Softteknik Bilişim',
    'website': "https://softteknik.com",
    'license': 'AGPL-3',
    'category': 'Website/Website',
    'version': '19.0.1.0.0',
    
    'depends': ['website_sale', 'crm'],
    
    'data': [       
       
        'views/crm_lead_views.xml',
        'views/website_quote_templates.xml',
        # 'views/templates.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
