import json
import os
import tempfile
import zipfile

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestZipVerification(TransactionCase):

    def setUp(self):
        super().setUp()
        self.job = self.env['backup.job'].new({
            'database_name': 'test_db',
            'backup_format': 'zip',
        })

    def _tmp(self, suffix='.zip'):
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        return path

    def _write_zip(self, path, db_name='test_db'):
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('dump.sql', b'-- pg dump line\n' * 50)
            zf.writestr('manifest.json', json.dumps({'db_name': db_name}))

    def _corrupt_byte(self, path, offset):
        with open(path, 'rb') as f:
            data = bytearray(f.read())
        data[offset] = (data[offset] + 1) % 256
        with open(path, 'wb') as f:
            f.write(data)

    def test_valid_zip_passes(self):
        path = self._tmp()
        try:
            self._write_zip(path)
            self.job._verify_zip_backup(path)
        finally:
            os.unlink(path)

    def test_corrupt_zip_rejected(self):
        """Regression (P0 #1): testzip() returns the corrupt entry name; the
        return value must be checked, not discarded, or a CRC-bad ZIP is
        silently reported as verified."""
        path = self._tmp()
        try:
            self._write_zip(path)
            self._corrupt_byte(path, 100)
            with self.assertRaises(UserError):
                self.job._verify_zip_backup(path)
        finally:
            os.unlink(path)

    def test_missing_dump_sql_rejected(self):
        path = self._tmp()
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
                zf.writestr('manifest.json', json.dumps({'db_name': 'test_db'}))
            with self.assertRaises(UserError):
                self.job._verify_zip_backup(path)
        finally:
            os.unlink(path)

    def test_missing_manifest_rejected(self):
        path = self._tmp()
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
                zf.writestr('dump.sql', b'-- dump\n')
            with self.assertRaises(UserError):
                self.job._verify_zip_backup(path)
        finally:
            os.unlink(path)

    def test_manifest_db_name_mismatch_rejected(self):
        path = self._tmp()
        try:
            self._write_zip(path, db_name='other_db')
            with self.assertRaises(UserError):
                self.job._verify_zip_backup(path)
        finally:
            os.unlink(path)

    def test_non_zip_rejected(self):
        path = self._tmp()
        try:
            with open(path, 'wb') as f:
                f.write(b'not a zip archive at all')
            with self.assertRaises(UserError):
                self.job._verify_zip_backup(path)
        finally:
            os.unlink(path)
