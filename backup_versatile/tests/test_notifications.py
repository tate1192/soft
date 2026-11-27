from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestBackupNotifications(TransactionCase):

    def test_notification_uses_odoo19_mail_api_and_active_server(self):
        config = self.env['backup.config'].new({
            'name': 'Notification test',
            'database_name': self.env.cr.dbname,
            'notification_emails': 'ops@example.com',
        })
        job = self.env['backup.job'].new({
            'config_id': config,
            'database_name': self.env.cr.dbname,
            'backup_format': 'zip',
            'status': 'success',
        })
        inactive = self.env['ir.mail_server'].create({
            'name': 'Inactive backup mail server',
            'smtp_host': 'inactive.example.com',
            'active': False,
            'sequence': 1,
        })
        active = self.env['ir.mail_server'].create({
            'name': 'Active backup mail server',
            'smtp_host': 'active.example.com',
            'smtp_user': 'backup@example.com',
            'active': True,
            'sequence': 2,
        })
        sentinel = object()
        with patch.object(type(active), '_build_email__', return_value=sentinel) as build, \
                patch.object(type(active), 'send_email', return_value='message-id') as send:
            config._send_notification(job, 'success')

        build.assert_called_once()
        send.assert_called_once_with(sentinel, mail_server_id=active.id)
        self.assertNotEqual(active.id, inactive.id)
