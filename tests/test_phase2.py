"""Phase 2: Facebook channel, one email for IG + FB, per-channel approvals and ledger."""

import html
import json
import os
import unittest
from unittest import mock

import draft
import publish
from core import approval, blog, copy, http, ledger, meta
from channels import facebook, instagram
from tests import test_pipeline as tp

ID = "700000000009"
ART = {"id": f"gid://shopify/Article/{ID}", "handle": "some-post", "title": "Some Post",
       "publishedAt": "2026-11-02T10:00:00Z", "isPublished": True, "image": {"url": "https://img"}}
CONTENT = {"caption": "Ein Kopfteil ist mehr als Rückenlehne.\n\nIm Journal: Formen und Pflege.\n\nLink in bio.",
           "hashtags": ["MaisonDeCocon", "SchlafNeuGedacht", "Kopfteil", "Interior", "Extra1", "Extra2", "Extra3"]}
UTM_URL = "https://www.maisondecocon.com/blogs/blog/some-post?utm_source=facebook&utm_medium=social&utm_campaign=some-post"


def drafts_for(caption="cap"):
    ig = instagram.build_draft(ART, "https://www.maisondecocon.com/blogs/blog/some-post",
                               {"caption": caption + "\n\nLink in bio.", "hashtags": ["A", "B"]}, f"generated/ig-{ID}.jpg")
    fb = facebook.build_draft(ART, UTM_URL, {"caption": caption + "\n\nLink in bio.", "hashtags": ["A", "B"]}, f"generated/ig-{ID}.jpg")
    return {"ig": ig, "fb": fb}


class FacebookCopy(unittest.TestCase):
    def test_link_in_bio_is_replaced_by_the_utm_link_and_hashtags_are_fewer(self):
        msg = copy.facebook_message(CONTENT["caption"], CONTENT["hashtags"], UTM_URL)
        self.assertNotIn("bio", msg.lower())
        self.assertIn(f"Zum Artikel: {UTM_URL}", msg)
        self.assertTrue(msg.startswith("Ein Kopfteil ist mehr als Rückenlehne."))
        self.assertIn("Im Journal: Formen und Pflege.", msg)
        tags = [w for w in msg.split() if w.startswith("#")]
        self.assertEqual(tags, ["#MaisonDeCocon", "#SchlafNeuGedacht", "#Kopfteil", "#Interior"])
        self.assertLess(len(tags), len(CONTENT["hashtags"]))

    def test_variants_of_the_call_to_action_and_no_cta_at_all(self):
        for cta in ("Link in Bio.", "Link in der Bio für mehr.", "Mehr dazu: Link in bio!"):
            self.assertNotIn("bio", copy.facebook_message(f"Text.\n\n{cta}", ["A"], UTM_URL).lower())
        self.assertIn("Text ohne CTA.", copy.facebook_message("Text ohne CTA.", ["A"], UTM_URL))

    def test_utm_parameters(self):
        with mock.patch.dict(os.environ, {"STOREFRONT_DOMAIN": "www.maisondecocon.com"}):
            self.assertEqual(facebook.article_url_with_utm("blog", "some-post"),
                             "https://www.maisondecocon.com/blogs/blog/some-post"
                             "?utm_source=facebook&utm_medium=social&utm_campaign=some-post")

    def test_caption_prompt_asks_for_german(self):
        import inspect
        self.assertIn("in\nGerman", inspect.getsource(copy.generate_instagram_caption))

    def test_draft_has_message_not_image_url(self):
        d = drafts_for()["fb"]
        self.assertEqual((d["channel"], d["article_id"], d["article_url"]), ("fb", ID, UTM_URL))
        self.assertIn("Zum Artikel:", d["message"])
        self.assertNotIn("image_url", d)


class DraftEmail(tp.InTmp):
    def run_process(self, channels=("ig", "fb"), art=ART):
        sent = []
        d = {c: v for c, v in drafts_for().items() if c in channels}
        with mock.patch.object(draft, "build_drafts", return_value=d), \
                mock.patch.object(approval, "_send", side_effect=lambda subj, body: sent.append(body)):
            draft.process(art, "m", channels)
        return sent

    def test_one_email_with_both_previews_and_four_buttons(self):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"}):
            sent = self.run_process()
        self.assertEqual(len(sent), 1)
        body = html.unescape(sent[0])
        self.assertIn("Instagram", body)
        self.assertIn("Facebook", body)
        self.assertIn("Zum Artikel:", body)
        for label, title in (("Approve both", f"approve%3A{ID}%3Aig%2Bfb"), ("IG only", f"approve%3A{ID}%3Aig"),
                             ("FB only", f"approve%3A{ID}%3Afb"), ("Reject", f"reject%3A{ID}%3Aig%2Bfb")):
            self.assertIn(f">{label}</a>", body)
            self.assertIn(f"title={title}&", sent[0].replace("&amp;", "&") + "&")
        self.assertEqual(sent[0].count("<a href"), 4)

    def test_both_channels_are_persisted_as_separate_draft_entries(self):
        self.run_process()
        data = ledger.load()[ID]
        self.assertEqual((data["ig"]["status"], data["fb"]["status"]), ("draft", "draft"))
        self.assertIn("full_caption", ledger.read_draft(ID, "ig"))
        self.assertIn("message", ledger.read_draft(ID, "fb"))

    def test_only_the_undrafted_channel_gets_two_buttons(self):
        sent = self.run_process(("fb",))
        self.assertEqual(sent[0].count("<a href"), 2)
        self.assertNotIn("Instagram</h3>", sent[0])
        self.assertNotIn("ig", ledger.load()[ID])

    def test_pending_section_lists_each_combination_with_its_approve_link(self):
        other = "700000000010"
        ledger.save({other: {"ig": {"status": "draft"}, "fb": {"status": "draft"}},
                     "700000000011": {"fb": {"status": "draft"}}, "700000000012": {"ig": {"status": "published"}}})
        for aid, ch in ((other, "ig"), (other, "fb"), ("700000000011", "fb")):
            ledger.write_draft(aid, ch, {"title": f"T{aid}", "handle": f"h{aid}"})
        body = self.run_process()[0]
        text = html.unescape(body)
        self.assertIn("Pending approvals (2)", text)
        self.assertIn(f"title=approve%3A{other}%3Aig%2Bfb", body)
        self.assertIn("title=approve%3A700000000011%3Afb", body)
        self.assertNotIn("700000000012", body)
        self.assertIn("Approve both", text)


class ApprovePaths(tp.InTmp):
    """publish.do_approve / main, with the platform calls faked."""

    def seed(self, **statuses):
        data = {ID: {c: {"status": s, "handle": "some-post", "article_id": ID, "url": None, "post_id": None}
                     for c, s in statuses.items()}}
        ledger.save(data)
        for c, d in drafts_for().items():
            ledger.write_draft(ID, c, d)
        self.calls = {"ig": [], "fb": []}

    def fake(self, channel, fail=None, definite=True):
        def publish_fn(d, mark, revert):
            self.calls[channel].append(d["image_url"])
            mark("c1" if channel == "ig" else None)
            if fail:
                if definite:
                    revert()
                    raise http.ApiError(fail)
                raise TimeoutError(fail)
            return {"post_id": f"{channel}-9", "url": f"https://{channel}/9", "timestamp": "t"}
        m = mock.Mock()
        m.publish.side_effect = publish_fn
        m.after_publish.return_value = None
        return m

    def approve(self, title, ig=None, fb=None, scopes=None, extra_env=None):
        ig, fb = ig or self.fake("ig"), fb or self.fake("fb")
        env = {**tp.MainFlow.env, "TITLE": title, "RESULT_FILE": "result.md", "GITHUB_REPOSITORY": "o/r", **(extra_env or {})}
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict(publish.CHANNELS, {"ig": ig, "fb": fb}), \
                mock.patch.object(meta, "preflight", return_value={}) as pf, \
                mock.patch.object(approval, "_send") as send:
            try:
                publish.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, pf, send

    def status(self, c):
        return ledger.load()[ID][c]["status"]

    def test_approve_both_publishes_both_and_records_each_separately(self):
        self.seed(ig="draft", fb="draft")
        code, pf, _ = self.approve(f"approve:{ID}:ig+fb")
        self.assertEqual(code, 0)
        data = ledger.load()[ID]
        self.assertEqual((data["ig"]["status"], data["ig"]["url"]), ("published", "https://ig/9"))
        self.assertEqual((data["fb"]["status"], data["fb"]["url"]), ("published", "https://fb/9"))
        self.assertEqual((data["ig"]["post_id"], data["fb"]["post_id"]), ("ig-9", "fb-9"))

    def test_ig_only_leaves_fb_as_draft(self):
        self.seed(ig="draft", fb="draft")
        code, _, _ = self.approve(f"approve:{ID}:ig")
        self.assertEqual((code, self.status("ig"), self.status("fb")), (0, "published", "draft"))
        self.assertEqual(self.calls["fb"], [])

    def test_fb_only_leaves_ig_as_draft(self):
        self.seed(ig="draft", fb="draft")
        code, _, _ = self.approve(f"approve:{ID}:fb")
        self.assertEqual((code, self.status("ig"), self.status("fb")), (0, "draft", "published"))
        self.assertEqual(self.calls["ig"], [])

    def test_reject_rejects_both_and_publishes_nothing(self):
        self.seed(ig="draft", fb="draft")
        code, pf, _ = self.approve(f"reject:{ID}:ig+fb")
        self.assertEqual((code, self.status("ig"), self.status("fb")), (0, "rejected", "rejected"))
        pf.assert_not_called()  # a reject needs no token

    def test_partial_failure_ig_ok_fb_fails_then_retry_only_fb(self):
        self.seed(ig="draft", fb="draft")
        code, _, send = self.approve(f"approve:{ID}:ig+fb", fb=self.fake("fb", fail="Facebook says no"))
        self.assertEqual(code, 1)
        self.assertEqual((self.status("ig"), self.status("fb")), ("published", "draft"))  # FB reverted, retryable
        subject, body = send.call_args[0]
        self.assertIn("Facebook says no", html.unescape(body))
        self.assertIn(f"article={ID}, channel=fb", html.unescape(body))                    # tells how to retry only FB
        ig_second = self.fake("ig")
        code, _, _ = self.approve(f"approve:{ID}:ig+fb", ig=ig_second)                    # retry the whole approval
        self.assertEqual(code, 0)
        self.assertEqual((self.status("ig"), self.status("fb")), ("published", "published"))
        self.assertEqual(self.calls["ig"], [self.calls["ig"][0]])                         # IG posted exactly once
        ig_second.publish.assert_not_called()

    def test_retry_only_fb_via_the_fb_link(self):
        self.seed(ig="published", fb="draft")
        ig = self.fake("ig")
        code, _, _ = self.approve(f"approve:{ID}:fb", ig=ig)
        self.assertEqual((code, self.status("fb")), (0, "published"))
        ig.publish.assert_not_called()

    def test_ambiguous_fb_failure_blocks_retry_and_shows_the_ledger_fix(self):
        self.seed(ig="draft", fb="draft")
        code, _, send = self.approve(f"approve:{ID}:fb", fb=self.fake("fb", fail="read timed out", definite=False))
        self.assertEqual((code, self.status("fb")), (1, "publishing"))
        body = html.unescape(send.call_args[0][1])
        self.assertIn(f'state/ledger.json -> "{ID}" -> "fb"', body)
        self.assertIn("the Facebook Page", body)
        code, _, _ = self.approve(f"approve:{ID}:fb")  # a retry must not post again
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls["fb"]), 1)

    def test_rerun_does_nothing_for_either_channel(self):
        self.seed(ig="draft", fb="draft")
        self.approve(f"approve:{ID}:ig+fb")
        before = (ledger.load(), len(self.calls["ig"]), len(self.calls["fb"]))
        code, _, _ = self.approve(f"approve:{ID}:ig+fb")
        self.assertEqual(code, 0)
        self.assertEqual((ledger.load(), len(self.calls["ig"]), len(self.calls["fb"])), before)

    def test_image_url_is_built_from_the_current_repo_for_both_channels(self):
        self.seed(ig="draft", fb="draft")
        self.approve(f"approve:{ID}:ig+fb", extra_env={"GITHUB_REPOSITORY": "dstark-maison/mdc-social-pipeline"})
        expected = f"https://raw.githubusercontent.com/dstark-maison/mdc-social-pipeline/main/generated/ig-{ID}.jpg"
        self.assertEqual((self.calls["ig"], self.calls["fb"]), ([expected], [expected]))

    def test_preflight_scopes_depend_on_the_channels_of_the_run(self):
        self.seed(ig="draft", fb="draft")
        _, pf, _ = self.approve(f"approve:{ID}:ig")
        self.assertNotIn("pages_manage_posts", pf.call_args[0][0])
        self.seed(ig="draft", fb="draft")
        _, pf, _ = self.approve(f"approve:{ID}:fb")
        self.assertIn("pages_manage_posts", pf.call_args[0][0])
        self.seed(ig="draft", fb="draft")
        _, pf, _ = self.approve(f"approve:{ID}:ig+fb")
        self.assertIn("pages_manage_posts", pf.call_args[0][0])
        self.assertEqual(meta.scopes_for(["ig"]), meta.REQUIRED_SCOPES)


GOOD = {"data": {"is_valid": True, "type": "SYSTEM_USER", "expires_at": 0,
                 "scopes": ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"]}}


class FakeResp:
    def __init__(self, data):
        self._d, self.ok, self.status_code, self.text = data, True, 200, json.dumps(data)

    def json(self):
        return self._d


class ScopesPerRun(tp.InTmp):
    TOKEN = "EAAB-system-user-token-value"

    def run_main(self, module, title=None, scopes=GOOD):
        env = {**tp.MainFlow.env, "ACCESS_TOKEN": self.TOKEN, "RESULT_FILE": "result.md", "GITHUB_REPOSITORY": "o/r"}
        if title:
            env["TITLE"] = title
        with mock.patch.dict(os.environ, env), mock.patch.object(http, "request", return_value=FakeResp(scopes)), \
                mock.patch.object(approval, "_send") as send:
            try:
                module.main()
                return 0, send
            except SystemExit as e:
                return e.code, send

    def seed(self):
        ledger.save({ID: {"ig": {"status": "draft", "handle": "h"}, "fb": {"status": "draft", "handle": "h"}}})
        for c, d in drafts_for().items():
            ledger.write_draft(ID, c, d)

    def test_token_without_pages_manage_posts_still_publishes_ig_only(self):
        self.seed()
        ig = mock.Mock()
        ig.publish.return_value = {"post_id": "9", "url": "u", "timestamp": "t"}
        ig.after_publish.return_value = None
        with mock.patch.dict(publish.CHANNELS, {"ig": ig}):
            code, _ = self.run_main(publish, f"approve:{ID}:ig")
        self.assertEqual(code, 0)

    def test_fb_run_without_pages_manage_posts_stops_and_names_the_scope(self):
        self.seed()
        fb = mock.Mock()
        with mock.patch.dict(publish.CHANNELS, {"fb": fb}):
            for title in (f"approve:{ID}:fb", f"approve:{ID}:ig+fb"):
                code, send = self.run_main(publish, title)
                self.assertEqual(code, 1)
                self.assertIn("pages_manage_posts", html.unescape(send.call_args[0][1]))
        fb.publish.assert_not_called()
        self.assertEqual(ledger.load()[ID]["fb"]["status"], "draft")

    def test_draft_run_requires_pages_manage_posts(self):
        env = {k: "x" * 10 for k in draft.REQUIRED_ENV}
        with mock.patch.dict(os.environ, env), mock.patch.object(http, "request", return_value=FakeResp(GOOD)), \
                mock.patch.object(approval, "_send") as send, mock.patch.object(draft.blog, "fetch_recent_articles") as fetch:
            with self.assertRaises(SystemExit):
                draft.main()
        fetch.assert_not_called()
        self.assertIn("pages_manage_posts", html.unescape(send.call_args[0][1]))


class FacebookChannel(tp.InTmp):
    PAGE_TOKEN = "EAAG-page-token-very-secret"

    def setUp(self):
        super().setUp()
        meta._page_cache.clear()
        self.addCleanup(meta._page_cache.clear)
        self.env = mock.patch.dict(os.environ, {"ACCESS_TOKEN": "EAAB-system-user-token", "IG_USER_ID": "IG1",
                                                "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def graph(self, photo_name=None, post_error=None, calls=None):
        calls = calls if calls is not None else []

        def fake(method, url, label, **kw):
            calls.append((method, url.rsplit("/", 2)[-2:], label, kw.get("headers", {}).get("Authorization"), kw.get("data")))
            if label == "meta-me-accounts":
                return FakeResp({"data": [{"id": "OTHER"}, {"id": "PAGE1"}]})
            if label == "meta-page":
                ig = "IG1" if "PAGE1" in url else "IGX"
                return FakeResp({"id": url.rsplit("/", 1)[-1], "name": "Maison de Cocon", "access_token": self.PAGE_TOKEN,
                                 "instagram_business_account": {"id": ig}})
            if label == "fb-photo-publish":
                if post_error:
                    raise http.ApiError(post_error)
                return FakeResp({"id": "PH1", "post_id": "PAGE1_PH1"})
            if label == "fb-readback":
                return FakeResp({"id": "PH1", "name": photo_name, "link": "https://facebook.com/photo?fbid=PH1",
                                 "created_time": "2026-11-02T10:00:00+0000"})
            raise AssertionError(label)
        return fake, calls

    def d(self):
        d = drafts_for()["fb"]
        d["image_url"] = "https://raw/x.jpg"
        return d

    def test_page_is_found_by_the_linked_instagram_account_and_token_is_runtime_only(self):
        fake, calls = self.graph()
        with mock.patch.object(http, "request", fake):
            pid, name, token = meta.page_credentials()
        self.assertEqual((pid, name, token), ("PAGE1", "Maison de Cocon", self.PAGE_TOKEN))
        self.assertEqual(http.redact(f"leak {self.PAGE_TOKEN}"), "leak ***")  # registered for redaction

    def test_no_linked_page_is_a_clear_error(self):
        def fake(method, url, label, **kw):
            if label == "meta-me-accounts":
                return FakeResp({"data": [{"id": "OTHER"}]})
            return FakeResp({"id": "OTHER", "access_token": "t" * 12, "instagram_business_account": {"id": "nope"}})
        with mock.patch.object(http, "request", fake):
            with self.assertRaises(meta.TokenError):
                meta.page_credentials()

    def test_publish_posts_photo_with_url_and_message_then_reads_back(self):
        d = self.d()
        fake, calls = self.graph(photo_name=d["message"])
        marks = []
        with mock.patch.object(http, "request", fake):
            result = facebook.publish(d, lambda c: marks.append("mark"), lambda: marks.append("revert"))
        self.assertEqual(result, {"post_id": "PH1", "url": "https://facebook.com/photo?fbid=PH1",
                                  "timestamp": "2026-11-02T10:00:00+0000"})
        post = next(c for c in calls if c[2] == "fb-photo-publish")
        self.assertEqual(post[4], {"url": "https://raw/x.jpg", "message": d["message"]})
        self.assertEqual(post[3], f"Bearer {self.PAGE_TOKEN}")        # Page token, as a header
        self.assertEqual(marks, ["mark"])
        self.assertLess([c[2] for c in calls].index("meta-page"), [c[2] for c in calls].index("fb-photo-publish"))

    def test_readback_with_a_different_message_fails_after_marking_publishing(self):
        fake, _ = self.graph(photo_name="something else")
        marks = []
        with mock.patch.object(http, "request", fake), mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                facebook.publish(self.d(), lambda c: marks.append("mark"), lambda: marks.append("revert"))
        self.assertEqual(marks, ["mark"])  # stays `publishing` -> no automatic second post

    def test_definite_facebook_error_reverts(self):
        fake, _ = self.graph(post_error="(#200) Permissions error")
        marks = []
        with mock.patch.object(http, "request", fake):
            with self.assertRaises(http.ApiError):
                facebook.publish(self.d(), lambda c: marks.append("mark"), lambda: marks.append("revert"))
        self.assertEqual(marks, ["mark", "revert"])

    def test_page_token_never_reaches_ledger_email_or_issue_comment(self):
        ledger.save({ID: {"fb": {"status": "draft", "handle": "h"}}})
        ledger.write_draft(ID, "fb", drafts_for()["fb"])
        fake, _ = self.graph(post_error=f"boom {self.PAGE_TOKEN} boom")
        env = {**tp.MainFlow.env, "TITLE": f"approve:{ID}:fb", "RESULT_FILE": "result.md", "ACCESS_TOKEN": "EAAB-system-user-token"}
        with mock.patch.dict(os.environ, env), mock.patch.object(http, "request", fake), \
                mock.patch.object(meta, "preflight", return_value={}), mock.patch.object(approval, "_send") as send:
            with self.assertRaises(SystemExit):
                publish.main()
        everything = send.call_args[0][1] + open("result.md", encoding="utf-8").read() + json.dumps(ledger.load())
        self.assertNotIn(self.PAGE_TOKEN, everything)
        self.assertNotIn("EAAB-system-user-token", everything)


if __name__ == "__main__":
    unittest.main()
