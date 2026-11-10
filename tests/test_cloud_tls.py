"""cloud/tls.py: the cloud image's TLS mode (Vast.ai). The image itself is
tested by cloud/smoke.sh in CI; this covers the pair checks and the config
it hands Caddy."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("cloud_tls", os.path.join(ROOT, "cloud", "tls.py"))
tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tls)

# What `caddy adapt` makes of cloud/Caddyfile (Caddy 2.10.2), trimmed.
ADAPTED = {
    "admin": {"disabled": True, "config": {"persist": False}},
    "apps": {"http": {"servers": {"srv0": {
        "listen": [":8188"],
        "routes": [{"handle": [{"handler": "static_response", "status_code": 401}]}],
        "automatic_https": {"disable": True},
    }}}},
}


def make_pair(dir, name, *extra):
    key, cert = os.path.join(dir, f"{name}.key"), os.path.join(dir, f"{name}.crt")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-days", "1",
         "-subj", "/CN=couchy-session", "-keyout", key, "-out", cert, *extra],
        check=True, capture_output=True,
    )
    with open(cert) as c, open(key) as k:
        return c.read(), k.read()


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make certificates")
class TLSTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cert, cls.key = make_pair(cls.dir, "a", "-nodes")
        cls.other_cert, cls.other_key = make_pair(cls.dir, "b", "-nodes")
        _, cls.encrypted_key = make_pair(cls.dir, "c", "-passout", "pass:secret")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def run_tls(self, env):
        out, err = io.StringIO(), io.StringIO()
        code = tls.main(env=env, stdin=io.StringIO(json.dumps(ADAPTED)), stdout=out, stderr=err)
        for secret in (self.key, self.other_key, self.encrypted_key):
            body = secret.splitlines()[1]
            self.assertNotIn(body, err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def assertRefused(self, env, words):
        code, out, err = self.run_tls(env)
        self.assertEqual(code, 64)
        self.assertEqual(out, "")
        self.assertIn(words, err)
        self.assertIn("not starting", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_pair_serves_exactly_that_certificate(self):
        code, out, err = self.run_tls({"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": self.key})
        self.assertEqual((code, err), (0, ""))
        config = json.loads(out)
        server = config["apps"]["http"]["servers"]["srv0"]
        self.assertEqual(server["tls_connection_policies"], [{"certificate_selection": {"any_tag": [tls.TAG]}}])
        self.assertEqual(server["protocols"], ["h1", "h2"])
        self.assertEqual(server["automatic_https"], {"disable": True})
        self.assertEqual(server["listen"], [":8188"])
        self.assertEqual(server["routes"], ADAPTED["apps"]["http"]["servers"]["srv0"]["routes"])
        pems = config["apps"]["tls"]["certificates"]["load_pem"]
        self.assertEqual(pems, [{"certificate": self.cert, "key": self.key, "tags": [tls.TAG]}])
        self.assertEqual(config["admin"], ADAPTED["admin"])

    def test_escaped_newlines(self):
        env = {"COUCHY_TLS_CERT": self.cert.replace("\n", "\\n"), "COUCHY_TLS_KEY": self.key.replace("\n", "\\n")}
        code, out, _ = self.run_tls(env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["apps"]["tls"]["certificates"]["load_pem"][0]["key"], self.key)

    def test_half_pair(self):
        self.assertRefused({"COUCHY_TLS_CERT": self.cert}, "COUCHY_TLS_KEY is missing or empty")
        self.assertRefused({"COUCHY_TLS_KEY": self.key}, "COUCHY_TLS_CERT is missing or empty")
        self.assertRefused({"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": " "}, "COUCHY_TLS_KEY is missing")
        self.assertRefused({"COUCHY_TLS_CERT": "", "COUCHY_TLS_KEY": ""}, "are set but empty")

    def test_bad_certificate(self):
        self.assertRefused({"COUCHY_TLS_CERT": "not a cert", "COUCHY_TLS_KEY": self.key}, "isn't a PEM certificate")
        broken = self.cert.replace(self.cert.splitlines()[2], "A" * 64)
        self.assertRefused({"COUCHY_TLS_CERT": broken, "COUCHY_TLS_KEY": self.key}, "isn't a PEM certificate")

    def test_bad_key(self):
        self.assertRefused({"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": "not a key"}, "isn't a PEM private key")
        broken = self.key.replace(self.key.splitlines()[1], "A" * 64)
        self.assertRefused({"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": broken}, "COUCHY_TLS_KEY isn't")

    def test_key_of_another_certificate(self):
        self.assertRefused({"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": self.other_key},
                           "isn't the private key of COUCHY_TLS_CERT")

    def test_encrypted_key_fails_without_prompting(self):
        self.assertRefused({"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": self.encrypted_key}, "COUCHY_TLS_KEY isn't")

    def test_unreadable_caddy_config(self):
        err = io.StringIO()
        code = tls.main(env={"COUCHY_TLS_CERT": self.cert, "COUCHY_TLS_KEY": self.key},
                        stdin=io.StringIO(""), stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, 64)
        self.assertIn("couldn't read Caddy's adapted config", err.getvalue())


if __name__ == "__main__":
    unittest.main()
