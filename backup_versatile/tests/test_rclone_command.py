from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestRcloneCommand(TransactionCase):

    _SECRET_FLAGS = (
        '--s3-secret-access-key', '--s3-access-key-id',
        '--b2-key', '--b2-account',
        '--gcs-client-secret', '--gcs-client-id',
        '--azureblob-key', '--azureblob-account',
        '--webdav-pass', '--webdav-user',
    )

    def _cloud(self, **kw):
        values = {
            'name': 'cloud',
            'cloud_service': 's3',
            'bucket': 'mybucket',
            'access_key_id': 'AKIAEXAMPLE',
            'secret_access_key': 'sekretvalue',
            'region': 'us-east-1',
        }
        values.update(kw)
        return self.env['backup.provider.cloud'].new(values)

    def _cmd(self, cloud):
        return cloud._build_rclone_command(
            'copy', '/local/file.zip', cloud._remote_path('file.zip'),
        )

    def test_no_secret_flag_in_command_s3(self):
        """Regression (P0 #5): secrets must not leak via the process table, so
        no secret CLI flag may appear in the built rclone command."""
        cmd = self._cmd(self._cloud(cloud_service='s3'))
        for flag in self._SECRET_FLAGS:
            self.assertNotIn(flag, cmd, "secret flag %s must not be in cmd" % flag)

    def test_no_secret_flag_in_command_all_services(self):
        for service in ('aws_s3', 'b2', 'gcs', 'azure', 'webdav'):
            cmd = self._cmd(self._cloud(
                cloud_service=service, endpoint='https://w.example'))
            for flag in self._SECRET_FLAGS:
                self.assertNotIn(flag, cmd)

    def test_secret_in_env_s3(self):
        env = self._cloud(cloud_service='s3')._rclone_secret_env()
        self.assertEqual(env.get('RCLONE_S3_ACCESS_KEY_ID'), 'AKIAEXAMPLE')
        self.assertEqual(env.get('RCLONE_S3_SECRET_ACCESS_KEY'), 'sekretvalue')

    def test_secret_in_env_per_service(self):
        cases = {
            'b2': ('RCLONE_B2_ACCOUNT', 'RCLONE_B2_KEY'),
            'gcs': ('RCLONE_GCS_CLIENT_ID', 'RCLONE_GCS_CLIENT_SECRET'),
            'azure': ('RCLONE_AZUREBLOB_ACCOUNT', 'RCLONE_AZUREBLOB_KEY'),
            'webdav': ('RCLONE_WEBDAV_USER', 'RCLONE_WEBDAV_PASS'),
        }
        for service, (user_key, secret_key) in cases.items():
            env = self._cloud(cloud_service=service)._rclone_secret_env()
            self.assertEqual(env.get(user_key), 'AKIAEXAMPLE')
            self.assertEqual(env.get(secret_key), 'sekretvalue')

    def test_custom_remote_empty_env(self):
        cloud = self._cloud(use_custom_remote=True, custom_remote_name='myremote')
        self.assertEqual(cloud._rclone_secret_env(), {})

    def test_single_remote_arg_for_lsjson(self):
        """Regression: rclone ls/lsjson/deletefile take one remote argument;
        the builder must not append an empty destination positional."""
        cloud = self._cloud()
        remote = cloud._remote_path()
        cmd = cloud._build_rclone_command('lsjson', remote, '', extras=['--recursive'])
        self.assertNotIn('', cmd)
        self.assertEqual(cmd[-1], remote)

    def test_single_remote_arg_for_deletefile(self):
        cloud = self._cloud()
        remote = cloud._remote_path('x.zip')
        cmd = cloud._build_rclone_command('deletefile', remote, '')
        self.assertNotIn('', cmd)
        self.assertEqual(cmd[-1], remote)

    def test_copy_keeps_both_args(self):
        cloud = self._cloud()
        remote = cloud._remote_path('x.zip')
        cmd = cloud._build_rclone_command('copy', '/local/x.zip', remote)
        self.assertEqual(cmd[-2], '/local/x.zip')
        self.assertEqual(cmd[-1], remote)

    def test_nonsecret_flags_still_present(self):
        cmd = self._cmd(self._cloud(
            cloud_service='s3', region='us-east-1',
            acl='private', storage_class='STANDARD'))
        self.assertIn('--s3-region', cmd)
        self.assertIn('us-east-1', cmd)
        self.assertIn('--s3-acl', cmd)
        self.assertIn('--s3-provider', cmd)
