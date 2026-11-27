import base64
import datetime
import io
import json
import os
import struct
import tempfile
import time
import types
import zipfile
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

import odoo.addons.backup_versatile.models.backup_job as backup_job_module
from odoo.addons.backup_versatile.models.backup_job import (
    CRYPTOGRAPHY_AVAILABLE,
    _ENCRYPTION_CHUNK_SIZE,
    _ENCRYPTION_MAGIC,
    _ENCRYPTION_NONCE_SIZE,
    _ENCRYPTION_TAG_SIZE,
    _ENCRYPTION_V2_HEADER,
    _ENCRYPTION_VERSION,
    _PG_STDERR_LIMIT,
)


@tagged('backup_versatile', 'standard')
class TestBackupIntegrity(TransactionCase):
    """Regression coverage for backup completeness and authenticity."""

    def setUp(self):
        super().setUp()
        self.key_raw = base64.b64encode(os.urandom(32)).decode()
        config = self.env['backup.config'].new({
            'name': 'integrity',
            'database_name': 'test_db',
            'backup_format': 'zip',
            'encrypt_key': self.key_raw,
        })
        self.job = self.env['backup.job'].new({
            'config_id': config,
            'database_name': 'test_db',
            'backup_format': 'zip',
        })

    # ---------------------------------------------------------- dump streaming
    def test_custom_dump_is_streamed_to_provided_stream(self):
        payload = b'PGDMP' + b'x' * (2 * 1024 * 1024 + 17)
        process = types.SimpleNamespace(
            stdout=io.BytesIO(payload),
            stderr=io.BytesIO(b''),
            wait=lambda: 0,
        )
        output = io.BytesIO()
        with patch.object(backup_job_module, 'find_pg_tool', return_value='pg_dump'), \
                patch.object(backup_job_module, 'exec_pg_environ', return_value={}), \
                patch.object(backup_job_module.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(backup_job_module.subprocess, 'run', side_effect=AssertionError('run used')):
            self.job._dump_db('test_db', output, 'dump')

        self.assertEqual(output.getvalue(), payload)
        self.assertEqual(popen.call_args.kwargs['stdout'], backup_job_module.subprocess.PIPE)
        self.assertIn('--format=c', popen.call_args.args[0])

    def test_custom_dump_nonzero_exit_fails_with_bounded_stderr(self):
        process = types.SimpleNamespace(
            stdout=io.BytesIO(b'partial'),
            stderr=io.BytesIO(b'e' * (_PG_STDERR_LIMIT + 4096) + b'actual pg_dump failure'),
            wait=lambda: 7,
        )
        with patch.object(backup_job_module, 'find_pg_tool', return_value='pg_dump'), \
                patch.object(backup_job_module, 'exec_pg_environ', return_value={}), \
                patch.object(backup_job_module.subprocess, 'Popen', return_value=process):
            with self.assertRaises(UserError) as caught:
                self.job._dump_db('test_db', io.BytesIO(), 'dump')

        self.assertIn('exit code 7', str(caught.exception))
        self.assertIn('actual pg_dump failure', str(caught.exception))
        self.assertLessEqual(len(str(caught.exception)), _PG_STDERR_LIMIT + 100)

    def test_dump_verification_uses_pg_restore_list(self):
        with tempfile.NamedTemporaryFile() as dump:
            dump.write(b'PGDMP' + b'not enough to be a real archive')
            dump.flush()
            with patch.object(backup_job_module, 'find_pg_tool', return_value='/usr/bin/pg_restore') as find_tool, \
                    patch.object(backup_job_module, 'exec_pg_environ', return_value={}), \
                    patch.object(backup_job_module.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
                self.job._verify_dump_backup(dump.name)

            find_tool.assert_called_once_with('pg_restore')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/pg_restore', '--list', dump.name])
            self.assertEqual(run.call_args.kwargs['stdout'], backup_job_module.subprocess.DEVNULL)

    def test_dump_verification_rejects_pg_restore_failure(self):
        def fail(cmd, **kwargs):
            kwargs['stderr'].write(b'input file does not appear to be a valid archive')
            return types.SimpleNamespace(returncode=3)

        with tempfile.NamedTemporaryFile() as dump:
            dump.write(b'PGDMP' + b'corrupt')
            dump.flush()
            with patch.object(backup_job_module, 'find_pg_tool', return_value='pg_restore'), \
                    patch.object(backup_job_module, 'exec_pg_environ', return_value={}), \
                    patch.object(backup_job_module.subprocess, 'run', side_effect=fail):
                with self.assertRaisesRegex(UserError, 'pg_restore --list failed'):
                    self.job._verify_dump_backup(dump.name)

    # -------------------------------------------------------- filestore integrity
    @staticmethod
    def _filestore_manifest(files):
        return {
            'file_count': len(files),
            'total_size': sum(len(content) for content in files.values()),
            'files': [
                {'path': path, 'size': len(content)}
                for path, content in sorted(files.items())
            ],
        }

    def _prepare_dump(self, root, expected_files):
        dump_dir = os.path.join(root, 'dump')
        os.makedirs(dump_dir)
        with open(os.path.join(dump_dir, 'dump.sql'), 'wb') as dump:
            dump.write(b'-- database dump\n')
        manifest = {'db_name': 'test_db', 'filestore': self._filestore_manifest(expected_files)}
        with open(os.path.join(dump_dir, 'manifest.json'), 'w') as manifest_file:
            json.dump(manifest, manifest_file)
        return dump_dir

    @staticmethod
    def _materialize_filestore(root, files):
        filestore = os.path.join(root, 'filestore')
        for relative, content in files.items():
            path = os.path.join(filestore, *relative.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as output:
                output.write(content)
        return filestore

    def _write_archive(self, root, dump_dir, filestore):
        output = io.BytesIO()
        with patch.object(backup_job_module.odoo.tools.config, 'filestore', return_value=filestore):
            self.job._write_backup_zip(output, dump_dir, 'test_db')
        return output.getvalue()

    def _verify_archive_bytes(self, archive):
        with tempfile.NamedTemporaryFile(suffix='.zip') as backup:
            backup.write(archive)
            backup.flush()
            self.job._verify_zip_backup(backup.name)

    def test_filestore_complete_archive_roundtrip(self):
        files = {'aa/one': b'one', 'bb/two': b'two-two'}
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, files)
            filestore = self._materialize_filestore(root, files)
            archive = self._write_archive(root, dump_dir, filestore)
            self._verify_archive_bytes(archive)
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                self.assertEqual(
                    sorted(name for name in zf.namelist() if name.startswith('filestore/')),
                    ['filestore/aa/one', 'filestore/bb/two'],
                )

    def test_missing_filestore_with_database_references_is_rejected(self):
        expected = {'aa/one': b'one'}
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, expected)
            with self.assertRaisesRegex(UserError, 'missing or unmounted'):
                self._write_archive(root, dump_dir, os.path.join(root, 'missing'))

    def test_missing_filestore_without_references_is_legitimately_empty(self):
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, {})
            archive = self._write_archive(root, dump_dir, os.path.join(root, 'missing'))
            self._verify_archive_bytes(archive)

    def test_filestore_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, {})
            real_filestore = os.path.join(root, 'real_filestore')
            os.makedirs(real_filestore)
            link = os.path.join(root, 'filestore_link')
            os.symlink(real_filestore, link)
            with self.assertRaisesRegex(UserError, 'symbolic link'):
                self._write_archive(root, dump_dir, link)

    def test_incomplete_filestore_is_rejected(self):
        expected = {'aa/one': b'one', 'bb/two': b'two'}
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, expected)
            filestore = self._materialize_filestore(root, {'aa/one': b'one'})
            with self.assertRaisesRegex(UserError, 'missing 1 expected filestore'):
                self._write_archive(root, dump_dir, filestore)

    def test_unreferenced_filestore_files_are_archived_and_counted(self):
        expected = {'aa/one': b'one'}
        actual = dict(expected, **{'bb/orphan': b'orphan'})
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, expected)
            filestore = self._materialize_filestore(root, actual)
            archive = self._write_archive(root, dump_dir, filestore)
            self._verify_archive_bytes(archive)
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                manifest = json.loads(zf.read('manifest.json'))
            self.assertEqual(manifest['filestore']['file_count'], 2)
            self.assertEqual(manifest['filestore']['database_file_count'], 1)

    def test_unreadable_filestore_walk_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, {})
            filestore = os.path.join(root, 'filestore')
            os.makedirs(filestore)

            def broken_walk(top, onerror=None):
                onerror(PermissionError('filestore is unreadable'))
                if False:
                    yield None

            with patch.object(backup_job_module.odoo.tools.config, 'filestore', return_value=filestore), \
                    patch.object(backup_job_module.os, 'walk', side_effect=broken_walk):
                with self.assertRaises(PermissionError):
                    self.job._write_backup_zip(io.BytesIO(), dump_dir, 'test_db')

    def test_unreadable_filestore_file_is_not_silently_skipped(self):
        files = {'aa/one': b'one'}
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, files)
            filestore = self._materialize_filestore(root, files)
            original_write = zipfile.ZipFile.write

            def deny_filestore(zf, filename, *args, **kwargs):
                if filename.startswith(filestore):
                    raise PermissionError('attachment is unreadable')
                return original_write(zf, filename, *args, **kwargs)

            with patch.object(backup_job_module.odoo.tools.config, 'filestore', return_value=filestore), \
                    patch.object(zipfile.ZipFile, 'write', deny_filestore):
                with self.assertRaises(PermissionError):
                    self.job._write_backup_zip(io.BytesIO(), dump_dir, 'test_db')

    def test_filestore_file_removed_during_archive_is_not_skipped(self):
        files = {'aa/one': b'one'}
        with tempfile.TemporaryDirectory() as root:
            dump_dir = self._prepare_dump(root, files)
            filestore = self._materialize_filestore(root, files)
            original_write = zipfile.ZipFile.write

            def remove_filestore(zf, filename, *args, **kwargs):
                if filename.startswith(filestore):
                    os.unlink(filename)
                return original_write(zf, filename, *args, **kwargs)

            with patch.object(backup_job_module.odoo.tools.config, 'filestore', return_value=filestore), \
                    patch.object(zipfile.ZipFile, 'write', remove_filestore):
                with self.assertRaises(FileNotFoundError):
                    self.job._write_backup_zip(io.BytesIO(), dump_dir, 'test_db')

    def test_zip_verification_rejects_filestore_count_mismatch(self):
        manifest = {
            'db_name': 'test_db',
            'filestore': self._filestore_manifest({'aa/one': b'one'}),
        }
        with tempfile.NamedTemporaryFile(suffix='.zip') as backup:
            with zipfile.ZipFile(backup.name, 'w') as zf:
                zf.writestr('dump.sql', b'-- dump\n')
                zf.writestr('manifest.json', json.dumps(manifest))
            with self.assertRaisesRegex(UserError, 'count mismatch'):
                self.job._verify_zip_backup(backup.name)

    def test_zip_verification_rejects_wrong_filestore_file_with_same_count(self):
        manifest = {
            'db_name': 'test_db',
            'filestore': self._filestore_manifest({'aa/one': b'one'}),
        }
        with tempfile.NamedTemporaryFile(suffix='.zip') as backup:
            with zipfile.ZipFile(backup.name, 'w') as zf:
                zf.writestr('dump.sql', b'-- dump\n')
                zf.writestr('manifest.json', json.dumps(manifest))
                zf.writestr('filestore/bb/two', b'two')
            with self.assertRaisesRegex(UserError, 'missing expected filestore'):
                self.job._verify_zip_backup(backup.name)

    def test_filestore_manifest_uses_distinct_attachment_metadata(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ('aa/one', 3),
            ('aa/one', 3),
            ('bb/two', 4),
        ]
        manifest = self.job._get_filestore_manifest(cursor)
        self.assertEqual(manifest, self._filestore_manifest({
            'aa/one': b'one', 'bb/two': b'two!',
        }))
        cursor.execute.assert_called_once()

    # ------------------------------------------------------------ encryption v2
    def _require_cryptography(self):
        if not CRYPTOGRAPHY_AVAILABLE:
            self.skipTest('cryptography not installed')

    def _encrypt(self, plaintext, chunk_size=64):
        with tempfile.TemporaryDirectory() as root:
            plain = os.path.join(root, 'backup.zip')
            with open(plain, 'wb') as output:
                output.write(plaintext)
            with patch.object(backup_job_module, '_ENCRYPTION_CHUNK_SIZE', chunk_size):
                encrypted = self.job._encrypt_backup_file(plain)
                with open(encrypted, 'rb') as source:
                    data = source.read()
                output = os.path.join(root, 'decrypted.zip')
                with open(output, 'wb') as existing:
                    existing.write(b'old output')
                self.env['backup.job'].decrypt_backup_file(encrypted, output, self.key_raw)
                with open(output, 'rb') as decrypted:
                    self.assertEqual(decrypted.read(), plaintext)
            return data

    @staticmethod
    def _split_v2_chunks(data):
        chunks = []
        position = _ENCRYPTION_V2_HEADER.size
        while position < len(data):
            ct_len = int.from_bytes(data[position + 20:position + 24], 'little')
            end = position + 24 + ct_len
            chunks.append(data[position:end])
            position = end
        return chunks

    def _assert_decrypt_rejected(self, data):
        self._require_cryptography()
        from cryptography.exceptions import InvalidTag

        with tempfile.TemporaryDirectory() as root:
            encrypted = os.path.join(root, 'backup.enc')
            output = os.path.join(root, 'output.zip')
            with open(encrypted, 'wb') as source:
                source.write(data)
            with open(output, 'wb') as existing:
                existing.write(b'preserve me')
            with self.assertRaises(Exception) as caught:
                self.env['backup.job'].decrypt_backup_file(encrypted, output, self.key_raw)
            self.assertIsInstance(caught.exception, (UserError, InvalidTag))
            with open(output, 'rb') as existing:
                self.assertEqual(existing.read(), b'preserve me')
            self.assertEqual(set(os.listdir(root)), {'backup.enc', 'output.zip'})

    def test_v2_roundtrip_binds_multiple_chunks(self):
        self._require_cryptography()
        data = self._encrypt(b'a' * 150, chunk_size=64)
        magic, version, chunk_size, total_chunks = _ENCRYPTION_V2_HEADER.unpack_from(data)
        self.assertEqual((magic, version, chunk_size, total_chunks), (_ENCRYPTION_MAGIC, _ENCRYPTION_VERSION, 64, 3))
        self.assertEqual(len(self._split_v2_chunks(data)), 3)

    def test_v2_empty_plaintext_roundtrip(self):
        self._require_cryptography()
        data = self._encrypt(b'', chunk_size=64)
        self.assertEqual(_ENCRYPTION_V2_HEADER.unpack_from(data)[3], 1)

    def test_v2_reordered_chunks_are_rejected(self):
        self._require_cryptography()
        data = self._encrypt(b'a' * 150, chunk_size=64)
        chunks = [bytearray(chunk) for chunk in self._split_v2_chunks(data)]
        chunks[0], chunks[1] = chunks[1], chunks[0]
        # Rewrite the visible indexes too; the authenticated AAD must still
        # detect that the ciphertext chunks themselves were reordered.
        struct.pack_into('<Q', chunks[0], 0, 0)
        struct.pack_into('<Q', chunks[1], 0, 1)
        self._assert_decrypt_rejected(data[:_ENCRYPTION_V2_HEADER.size] + b''.join(chunks))

    def test_v2_duplicated_chunks_are_rejected(self):
        self._require_cryptography()
        data = self._encrypt(b'a' * 150, chunk_size=64)
        chunks = self._split_v2_chunks(data)
        chunks[1] = chunks[0]
        self._assert_decrypt_rejected(data[:_ENCRYPTION_V2_HEADER.size] + b''.join(chunks))

    def test_v2_header_chunk_count_and_final_flag_are_authenticated(self):
        self._require_cryptography()
        data = self._encrypt(b'a' * 150, chunk_size=64)
        chunks = self._split_v2_chunks(data)
        # Remove the real final chunk and claim that chunk 1 is final.  The
        # length is plausible, but its GCM tag was created with total=3 and
        # final=False, so authentication must still reject it.
        tampered = bytearray(data[:_ENCRYPTION_V2_HEADER.size] + b''.join(chunks[:2]))
        struct.pack_into('<Q', tampered, 10, 2)
        self._assert_decrypt_rejected(bytes(tampered))

    def test_v2_truncated_ciphertext_is_rejected_atomically(self):
        self._require_cryptography()
        data = self._encrypt(b'a' * 150, chunk_size=64)
        self._assert_decrypt_rejected(data[:-1])

    def test_v2_trailing_data_is_rejected(self):
        self._require_cryptography()
        data = self._encrypt(b'a' * 150, chunk_size=64)
        self._assert_decrypt_rejected(data + b'trailing')

    def test_v2_chunk_size_length_bounds_are_enforced(self):
        self._require_cryptography()
        original = self._encrypt(b'a' * 150, chunk_size=64)
        data = bytearray(original)
        struct.pack_into('<I', data, 6, 0)
        self._assert_decrypt_rejected(bytes(data))
        data = bytearray(original)
        struct.pack_into('<I', data, 6, _ENCRYPTION_CHUNK_SIZE + 1)
        self._assert_decrypt_rejected(bytes(data))

    def test_v2_ciphertext_length_bound_is_enforced(self):
        self._require_cryptography()
        data = bytearray(self._encrypt(b'a' * 150, chunk_size=64))
        first_chunk = _ENCRYPTION_V2_HEADER.size
        struct.pack_into('<I', data, first_chunk + 20, 64 + _ENCRYPTION_TAG_SIZE + 1)
        self._assert_decrypt_rejected(bytes(data))

    def test_v2_chunk_count_length_bound_is_enforced(self):
        self._require_cryptography()
        original = self._encrypt(b'a' * 150, chunk_size=64)
        data = bytearray(original)
        struct.pack_into('<Q', data, 10, 0)
        self._assert_decrypt_rejected(bytes(data))
        data = bytearray(original)
        struct.pack_into('<Q', data, 10, 4)
        self._assert_decrypt_rejected(bytes(data))

    def test_wrong_key_does_not_replace_existing_output(self):
        self._require_cryptography()
        from cryptography.exceptions import InvalidTag

        with tempfile.TemporaryDirectory() as root:
            plain = os.path.join(root, 'backup.zip')
            with open(plain, 'wb') as output:
                output.write(b'plaintext')
            encrypted = self.job._encrypt_backup_file(plain)
            output = os.path.join(root, 'output.zip')
            with open(output, 'wb') as existing:
                existing.write(b'preserve me')
            wrong_key = base64.b64encode(os.urandom(32)).decode()
            with self.assertRaises(InvalidTag):
                self.env['backup.job'].decrypt_backup_file(encrypted, output, wrong_key)
            with open(output, 'rb') as existing:
                self.assertEqual(existing.read(), b'preserve me')

    def test_legacy_v1_remains_readable(self):
        self._require_cryptography()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = base64.b64decode(self.key_raw)
        aesgcm = AESGCM(key)
        plaintext = b'legacy plaintext'
        nonce = os.urandom(_ENCRYPTION_NONCE_SIZE)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        legacy = (
            (64).to_bytes(4, 'little')
            + nonce
            + len(ciphertext).to_bytes(4, 'little')
            + ciphertext
        )
        with tempfile.TemporaryDirectory() as root:
            encrypted = os.path.join(root, 'legacy.enc')
            output = os.path.join(root, 'output.zip')
            with open(encrypted, 'wb') as source:
                source.write(legacy)
            self.env['backup.job'].decrypt_backup_file(encrypted, output, self.key_raw)
            with open(output, 'rb') as decrypted:
                self.assertEqual(decrypted.read(), plaintext)

    # ----------------------------------------------------- filenames/providers
    def _persisted_job(self, template=False):
        config = self.env['backup.config'].create({
            'name': 'filename config',
            'database_name': 'test_db',
            'backup_format': 'zip',
            'backup_name_template': template,
            'active': False,
        })
        return self.env['backup.job'].create({
            'config_id': config.id,
            'database_name': 'test_db',
            'backup_format': 'zip',
        })

    def test_default_filename_has_microseconds_and_job_identifier(self):
        job = self._persisted_job()
        fixed = datetime.datetime(2026, 11, 28, 12, 13, 14, 123456)
        with patch.object(backup_job_module.datetime, 'datetime') as clock:
            clock.now.return_value = fixed
            filename = job._generate_backup_filename()
        self.assertIn('2026_11_28_12_13_14_123456', filename)
        self.assertIn('_%d.zip' % job.id, filename)

    def test_same_microsecond_still_produces_unique_job_filenames(self):
        first = self._persisted_job()
        second = self._persisted_job()
        fixed = datetime.datetime(2026, 11, 28, 12, 13, 14, 123456)
        with patch.object(backup_job_module.datetime, 'datetime') as clock:
            clock.now.return_value = fixed
            first_name = first._generate_backup_filename()
            second_name = second._generate_backup_filename()
        self.assertNotEqual(first_name, second_name)

    def test_job_template_variable_is_exposed(self):
        job = self._persisted_job('{database}_{job}.{format}')
        filename = job._generate_backup_filename()
        self.assertIn('test_db_%d_' % job.id, filename)
        self.assertTrue(filename.endswith('.zip'))

    def test_invalid_filename_templates_are_rejected(self):
        job = self._persisted_job()
        for template in (
            '{unknown}.{format}',
            '{database.{format}',
            '{database!r}.{format}',
            'a' * 256,
        ):
            with self.subTest(template=template):
                job.config_id.backup_name_template = template
                with self.assertRaises(UserError):
                    job._generate_backup_filename()

    def test_empty_overlong_and_nul_filenames_are_rejected(self):
        for filename in ('', '   ', '___', '.', '..', 'a' * 256, 'bad\x00name.zip'):
            with self.subTest(filename=filename):
                with self.assertRaises(UserError):
                    self.job._sanitize_filename(filename)

    def test_template_path_separators_are_sanitized(self):
        job = self._persisted_job('{database}/../backup.{format}')
        filename = job._generate_backup_filename()
        self.assertNotIn('/', filename)
        self.assertNotIn('\\', filename)
        self.assertEqual(filename, os.path.basename(filename))

    def test_database_double_underscores_are_preserved_for_retention(self):
        job = self._persisted_job('{database}_{timestamp}_{job}.{format}')
        job.database_name = 'my__db'
        self.assertTrue(job._generate_backup_filename().startswith('my__db_'))

    def test_provider_results_use_model_and_record_id(self):
        def provider(model, record_id, result):
            return types.SimpleNamespace(
                _name=model,
                id=record_id,
                name='duplicate name',
                _get_destination_path=lambda: '',
                upload_backup=lambda path, filename: result,
            )

        providers = [
            provider('backup.provider.local', 1, {'success': True}),
            provider('backup.provider.local', 2, {'success': False, 'message': 'failed'}),
            provider('backup.provider.sftp', 1, {'success': True}),
        ]
        results = self.job._upload_to_providers_sequential('backup.zip', providers)
        self.assertEqual(set(results), {
            'backup.provider.local:1',
            'backup.provider.local:2',
            'backup.provider.sftp:1',
        })
        self.assertFalse(results['backup.provider.local:2']['success'])

    # ---------------------------------------------------------- temp ownership
    def test_stale_sweep_never_removes_live_owner_beyond_hard_max_age(self):
        with tempfile.TemporaryDirectory() as root:
            live = os.path.join(root, 'odoo_backup_live')
            dead = os.path.join(root, 'odoo_backup_dead')
            os.makedirs(live)
            os.makedirs(dead)
            with open(os.path.join(live, self.job._TEMP_OWNER_MARKER), 'w') as marker:
                marker.write(str(os.getpid()))
            with open(os.path.join(dead, self.job._TEMP_OWNER_MARKER), 'w') as marker:
                marker.write('99999999')
            old = time.time() - 3 * 24 * 3600
            os.utime(live, (old, old))
            os.utime(dead, (old, old))

            with patch.object(backup_job_module.tempfile, 'gettempdir', return_value=root):
                self.env['backup.job']._cleanup_stale_temp_files(
                    max_age_seconds=1, hard_max_age_seconds=1,
                )

            self.assertTrue(os.path.isdir(live))
            self.assertFalse(os.path.exists(dead))
