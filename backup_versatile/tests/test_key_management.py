import base64

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestEncryptionKeyManagement(TransactionCase):

    def test_existing_key_cannot_be_overwritten_accidentally(self):
        existing = base64.b64encode(b'x' * 32).decode()
        config = self.env['backup.config'].create({
            'name': 'Existing key',
            'database_name': self.env.cr.dbname,
            'active': False,
            'encrypt_backup': True,
            'encrypt_key': existing,
        })
        with self.assertRaises(UserError):
            config.action_generate_encryption_key()
        self.assertEqual(config.encrypt_key, existing)

    def test_new_key_is_32_random_bytes(self):
        config = self.env['backup.config'].new({
            'name': 'New key',
            'database_name': self.env.cr.dbname,
            'encrypt_backup': True,
        })
        config.action_generate_encryption_key()
        self.assertEqual(len(base64.b64decode(config.encrypt_key)), 32)
