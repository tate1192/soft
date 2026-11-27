import hashlib
import os
import shutil
import tempfile
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.backup_versatile.models import backup_provider_local


@tagged('backup_versatile', 'standard')
class TestLocalAtomicUpload(TransactionCase):
    """Local backup publication and deletion must be path-safe and atomic."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Provider = cls.env['backup.provider.local']

    def setUp(self):
        super().setUp()
        self.temp_dirs = []

    def tearDown(self):
        for path in self.temp_dirs:
            shutil.rmtree(path, ignore_errors=True)
        super().tearDown()

    def _temp_dir(self):
        path = tempfile.mkdtemp(prefix='bv_atomic_local_')
        self.temp_dirs.append(path)
        return path

    def _provider(self, directory):
        return self.Provider.create({
            'name': 'Atomic local provider',
            'backup_directory': directory,
            'create_subdirectories': False,
            'check_disk_space': False,
        })

    def _source(self, content=b'backup-content'):
        source_dir = self._temp_dir()
        source_path = os.path.join(source_dir, 'source.zip')
        with open(source_path, 'wb') as source_file:
            source_file.write(content)
        os.chmod(source_path, 0o644)
        return source_path

    def test_upload_is_verified_and_published_with_mode_0600(self):
        target_dir = self._temp_dir()
        provider = self._provider(target_dir)
        source_path = self._source()

        result = provider.upload_backup(source_path, 'database.zip')

        self.assertTrue(result['success'], result['message'])
        final_path = os.path.join(target_dir, 'database.zip')
        with open(source_path, 'rb') as source_file, open(final_path, 'rb') as final_file:
            self.assertEqual(
                hashlib.sha256(source_file.read()).digest(),
                hashlib.sha256(final_file.read()).digest(),
            )
        self.assertEqual(os.stat(final_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(target_dir), ['database.zip'])

    def test_verification_failure_cleans_partial_and_keeps_previous_final(self):
        target_dir = self._temp_dir()
        provider = self._provider(target_dir)
        source_path = self._source(b'new-content')
        final_path = os.path.join(target_dir, 'database.zip')
        with open(final_path, 'wb') as final_file:
            final_file.write(b'previous-good-backup')

        with patch.object(
            type(provider), '_verify_backup_integrity', return_value=False,
        ):
            result = provider.upload_backup(source_path, 'database.zip')

        self.assertFalse(result['success'])
        with open(final_path, 'rb') as final_file:
            self.assertEqual(final_file.read(), b'previous-good-backup')
        self.assertEqual(os.listdir(target_dir), ['database.zip'])

    def test_replace_failure_cleans_partial(self):
        target_dir = self._temp_dir()
        provider = self._provider(target_dir)
        source_path = self._source()

        with patch.object(
            backup_provider_local.os, 'replace',
            side_effect=OSError('simulated replace failure'),
        ):
            result = provider.upload_backup(source_path, 'database.zip')

        self.assertFalse(result['success'])
        self.assertEqual(os.listdir(target_dir), [])

    def test_delete_uses_exact_full_path_instead_of_basename_search(self):
        target_dir = self._temp_dir()
        subdir = os.path.join(target_dir, 'dated')
        os.mkdir(subdir)
        root_copy = os.path.join(target_dir, 'duplicate.zip')
        dated_copy = os.path.join(subdir, 'duplicate.zip')
        for path in (root_copy, dated_copy):
            with open(path, 'wb') as backup_file:
                backup_file.write(b'backup')
        provider = self._provider(target_dir)

        with patch.object(
            type(provider), '_find_backup_file',
            side_effect=AssertionError('basename search must not be used'),
        ):
            result = provider.delete_backup(dated_copy)

        self.assertTrue(result['success'], result['message'])
        self.assertTrue(os.path.exists(root_copy))
        self.assertFalse(os.path.exists(dated_copy))

    def test_delete_rejects_full_path_outside_backup_directory(self):
        target_dir = self._temp_dir()
        outside_dir = self._temp_dir()
        outside_path = os.path.join(outside_dir, 'outside.zip')
        with open(outside_path, 'wb') as outside_file:
            outside_file.write(b'backup')
        provider = self._provider(target_dir)

        result = provider.delete_backup(outside_path)

        self.assertFalse(result['success'])
        self.assertTrue(os.path.exists(outside_path))

    def test_delete_rejects_basename_only(self):
        target_dir = self._temp_dir()
        backup_path = os.path.join(target_dir, 'database.zip')
        with open(backup_path, 'wb') as backup_file:
            backup_file.write(b'backup')
        provider = self._provider(target_dir)

        result = provider.delete_backup('database.zip')

        self.assertFalse(result['success'])
        self.assertTrue(os.path.exists(backup_path))

    def test_listing_and_deletion_reject_symlinks(self):
        target_dir = self._temp_dir()
        outside_dir = self._temp_dir()
        outside_path = os.path.join(outside_dir, 'outside.zip')
        with open(outside_path, 'wb') as backup_file:
            backup_file.write(b'outside backup')
        link_path = os.path.join(target_dir, 'linked.zip')
        os.symlink(outside_path, link_path)
        provider = self._provider(target_dir)

        self.assertEqual(provider.list_backups(), [])
        result = provider.delete_backup(link_path)
        self.assertFalse(result['success'])
        self.assertTrue(os.path.lexists(link_path))
        self.assertTrue(os.path.exists(outside_path))
