odoo.define('your_module.quote_form', function (require) {
    'use strict';

    var publicWidget = require('web.public.widget');

    publicWidget.registry.QuoteForm = publicWidget.Widget.extend({
        selector: '.quote_request_form',
        events: {
            'click button[data-bs-toggle="modal"]': '_onClickModalButton',
        },

        _onClickModalButton: function (ev) {
            console.log('Modal butonu tıklandı!');
            // Modalın çalıştığından emin olmak için
            var modal = new bootstrap.Modal(document.getElementById('quoteModal'));
            modal.show();
        },
    });

    return publicWidget.registry.QuoteForm;
});