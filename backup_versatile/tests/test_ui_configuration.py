from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestBackupUiConfiguration(TransactionCase):

    def test_configuration_can_be_activated_and_custom_retention_is_editable(self):
        arch = self.env.ref('backup_versatile.view_backup_config_form').arch
        self.assertIn('name="active"', arch)
        self.assertIn(
            "retention_policy not in ['count', 'custom']", arch,
        )
        self.assertIn(
            "retention_policy not in ['days', 'custom']", arch,
        )

    def test_sftp_key_authentication_fields_are_available(self):
        arch = self.env.ref('backup_versatile.view_backup_provider_sftp_form').arch
        for field_name in ('active', 'auth_type', 'private_key', 'private_key_passphrase'):
            self.assertIn('name="%s"' % field_name, arch)

    def test_restore_menu_is_restricted(self):
        menu = self.env.ref('backup_versatile.menu_restore_backup')
        self.assertTrue(menu.group_ids)
