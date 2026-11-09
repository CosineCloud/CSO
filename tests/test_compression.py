import unittest
from compression_codec import compress_content,decompress_content


class CompressionTests(unittest.TestCase):
    def test_all_codecs_round_trip_and_auto_selects_smallest(self):
        content=b'server storage data '*2000
        sizes=[]
        for mode in ('zip','bz2','xz'):
            packed,codec=compress_content(content,mode)
            self.assertEqual(codec,mode)
            self.assertEqual(decompress_content(packed,codec,len(content)),content)
            sizes.append(len(packed))
            with self.assertRaises((ValueError,EOFError,OSError)):
                decompress_content(packed,codec,1)
        packed,codec=compress_content(content,'auto')
        self.assertEqual(len(packed),min(sizes))
        self.assertEqual(decompress_content(packed,codec,len(content)),content)
        self.assertEqual(compress_content(b'abc','auto'),(b'abc','none'))

    def test_compact_encryption_legacy_compatibility_and_tamper_detection(self):
        from cryptography.fernet import Fernet,InvalidToken
        from encrypted_payload import encrypt_content,decrypt_content
        cipher=Fernet(Fernet.generate_key())
        content=b'incompressible-like payload '*1000
        legacy=cipher.encrypt(content)
        compact=encrypt_content(cipher,content)
        self.assertLess(len(compact),len(legacy))
        self.assertEqual(decrypt_content(cipher,compact),content)
        self.assertEqual(decrypt_content(cipher,legacy),content)
        changed=bytearray(compact);changed[len(changed)//2]^=1
        with self.assertRaises(InvalidToken):decrypt_content(cipher,bytes(changed))
        changed=bytearray(compact);changed[0]^=1
        with self.assertRaises(InvalidToken):decrypt_content(cipher,bytes(changed))
