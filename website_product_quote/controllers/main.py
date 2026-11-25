# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class WebsiteProductQuoteV18(http.Controller):

    @http.route('/shop/quote/submit', type='http', auth='public', website=True, csrf=True, methods=['POST'])
    def quote_submit_crm(self, **post):
        # --- 1. Form Verilerini Güvenli Bir Şekilde Al ---
        company = post.get('company', '').strip()
        name = post.get('name', '').strip()
        email = post.get('email', '').strip()
        phone = post.get('phone', '').strip()
        product_id_str = post.get('product_id') # Formdan gelen ürün ID'si
        quantity_str = post.get('quantity', '').strip()
        description = post.get('description', '').strip()

        # --- UTM Kaynak/Ortam Güvenli Alma ---
        # ... (UTM kodları önceki gibi) ...
        source_id = False
        utm_source = request.env.ref('utm.utm_source_website', raise_if_not_found=False)
        if utm_source:
            source_id = utm_source.id
        else:
            _logger.warning("utm.utm_source_website XML ID'sine karşılık gelen utm.source kaydı bulunamadı.")

        medium_id = False
        utm_medium = request.env.ref('utm.utm_medium_website', raise_if_not_found=False)
        if utm_medium:
            medium_id = utm_medium.id
        else:
            _logger.warning("utm.utm_medium_website XML ID'sine karşılık gelen utm.medium kaydı bulunamadı.")

        # --- 3. Fırsat (Lead) Verilerini Hazırla (Başlangıç Değerleri) ---
        lead_vals = {
            'name': f" {product_id_str or 'Genel'}", # Geçici isim
            'partner_name': company or None,
            'contact_name': name or None,
            'email_from': email or None,
            'phone': phone or None,
            'description': description, # Ana açıklama (ürün bilgisi sonra eklenecek)
            'type': 'lead',
            'user_id': request.website.salesperson_id.id if request.website and request.website.salesperson_id else False,
            'team_id': request.website.salesteam_id.id if request.website and request.website.salesteam_id else False,
            #'website_id': request.website.id if request.website else False, # website_crm kuruluysa aktif edilebilir
            'source_id': source_id,
            'medium_id': medium_id,
        }

        # --- 4. Ürün Bilgilerini İşle ve lead_vals'ı Güncelle ---
        product = None
        product_info_for_description = ""

        if product_id_str:
            try:
                product_id = int(product_id_str)
                product = request.env['product.product'].sudo().browse(product_id)

                if not product.exists():
                    _logger.warning(f"Teklif talebi için ürün bulunamadı: ID {product_id}")
                    product_info_for_description = f"\n\n--- Talep Edilen Ürün ---\nÜrün ID: {product_id_str} (Bulunamadı)"
                    product = None
                else:
                    # ----- DEĞİŞİKLİK BURADA -----
                    # Ürün bulundu, lead ismini ve açıklama bilgisini GÜNCELLE
                    # product.name kullanarak sadece ürün adını al (kod olmadan)
                    product_name_only = product.name
                    lead_vals['name'] = f"{product_name_only} -" # İsmi güncelle (sadece ad ile)

                    # Açıklama için display_name (kod + ad) veya sadece name kullanılabilir, tercihinize bağlı
                    # product_info_for_description = f"\n\n--- Talep Edilen Ürün ---\nÜrün: {product.display_name}" # Kod + Ad
                    product_info_for_description = f"\n\n--- Talep Edilen Ürün ---\nÜrün: {product_name_only}" # Sadece Ad
                    # -----------------------------

                    product_info_for_description += f"\nReferans: {product.default_code or '-'}" # Referans Kodu hala eklenebilir

                    quantity = None
                    if quantity_str:
                        try:
                            quantity = float(quantity_str.replace(',', '.'))
                            product_info_for_description += f"\nMiktar: {quantity}"
                        except ValueError:
                            _logger.warning(f"Geçersiz miktar formatı: {quantity_str}")
                            product_info_for_description += f"\nMiktar: {quantity_str} (Geçersiz Format!)"

            except ValueError:
                _logger.warning(f"Geçersiz ürün ID formatı: {product_id_str}")
                product_info_for_description = f"\n\n--- Talep Edilen Ürün ---\nÜrün ID (Geçersiz): {product_id_str}"

        # Ürün bilgilerini ana açıklamaya ekle
        lead_vals['description'] = (lead_vals.get('description') or '') + product_info_for_description

        # --- 5. CRM Fırsatını Oluştur ---
        try:
            lead = request.env['crm.lead'].sudo().create(lead_vals)
            _logger.info(f"Web sitesi teklif formundan CRM Fırsatı oluşturuldu: ID {lead.id} - {lead.name}")

            return request.render('website_product_quote.quote_thank_you', {
                'lead': lead,
                'product': product
            })

        except Exception as e:
            _logger.error(f"Web sitesi teklif formundan CRM Fırsatı oluşturulurken hata: {e}", exc_info=True)
            referrer = request.httprequest.referrer
            return request.redirect(referrer + ('?' if '?' not in referrer else '&') + 'error=submit_failed')