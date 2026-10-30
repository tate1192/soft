import base64
import os
import tempfile

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


@tagged('backup_versatile', 'standard')
class TestEncryption(TransactionCase):

    def setUp(self):
        super().setUp()
        self.key_raw = base64.b64encode(os.urandom(32)).decode()
        config = self.env['backup.config'].new({
            'name': 'enc',
            'database_name': 'test_db',
            'encrypt_key': self.key_raw,
        })
        self.job = self.env['backup.job'].new({
            'config_id': config,
            'database_name': 'test_db',
            'backup_format': 'zip',
        })

    def _write_plaintext(self, path, size=4096):
        with open(path, 'wb') as f:
            f.write(os.urandom(size))

    def test_roundtrip(self):
        if not CRYPTOGRAPHY_AVAILABLE:
            self.skipTest("cryptography not installed")
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, 'backup.zip')
            self._write_plaintext(plain)
            enc = self.job._encrypt_backup_file(plain)
            out = os.path.join(d, 'decrypted.zip')
            self.env['backup.job'].decrypt_backup_file(enc, out, self.key_raw)
            with open(plain, 'rb') as a, open(out, 'rb') as b:
                self.assertEqual(a.read(), b.read())

    def test_ciphertext_differs(self):
        if not CRYPTOGRAPHY_AVAILABLE:
            self.skipTest("cryptography not installed")
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, 'backup.zip')
            self._write_plaintext(plain)
            enc = self.job._encrypt_backup_file(plain)
            with open(plain, 'rb') as a, open(enc, 'rb') as b:
                self.assertNotEqual(a.read(), b.read())

    def test_wrong_key_fails(self):
        if not CRYPTOGRAPHY_AVAILABLE:
            self.skipTest("cryptography not installed")
        from cryptography.exceptions import InvalidTag
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, 'backup.zip')
            self._write_plaintext(plain)
            enc = self.job._encrypt_backup_file(plain)
            wrong_key = base64.b64encode(os.urandom(32)).decode()
            out = os.path.join(d, 'out.zip')
            with self.assertRaises(InvalidTag):
                self.env['backup.job'].decrypt_backup_file(enc, out, wrong_key)

    def test_no_key_raises(self):
        if not CRYPTOGRAPHY_AVAILABLE:
            self.skipTest("cryptography not installed")
        config = self.env['backup.config'].new({
            'name': 'nokey', 'database_name': 'test_db', 'encrypt_key': False,
        })
        job = self.env['backup.job'].new({
            'config_id': config, 'database_name': 'test_db', 'backup_format': 'zip',
        })
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, 'b.zip')
            self._write_plaintext(plain)
            with self.assertRaises(UserError):
                job._encrypt_backup_file(plain)
