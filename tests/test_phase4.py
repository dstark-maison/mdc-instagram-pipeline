"""Phase 4: Reddit drafts (GSC + pasted findquestions + FAQ), manual publishing, privacy."""

import base64
import contextlib
import html
import io
import json
import os
import shutil
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import publish
import reddit_draft
import yaml
from core import approval, config, copy, gsc, http, ledger, questions, reddit_articles as ra, reddit_copy, reddit_store
from tests import test_pipeline as tp

HELD_ID = next(iter(config.HOLD_ARTICLE_IDS))


def faq_json(pairs):
    return ('<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs]}) + "</script>")


def art(i, handle, de, title, body_text, faq, aid=None, published=True):
    return {"id": aid or f"90000000000{i}", "handle": handle, "de_handle": de, "title": title, "de_title": None,
            "published": published, "published_at": "2026-10-01T10:00:00Z",
            "body": f"<p>{body_text}</p>" + faq_json(faq)}


INDEX = [
    art(1, "flannel-vs-flannelette-bedding", "winterbettwaesche-flanell-biber", "Winter Bedding: Flannel vs. Flannelette",
        "Flannel is woven cotton brushed on both sides. Flannelette is lighter. A typical flannel duvet cover weighs about 180 g/m2. " * 3,
        [("Is flannel warmer than flannelette?", "Flannel is usually warmer because it is heavier.")]),
    art(2, "duvet-sizes-for-every-bed", "bettdecken-groessen-tabelle", "Duvet Sizes: Which Size Fits Which Bed?",
        "In Germany a double bed usually takes a 200 x 200 cm duvet. A single duvet is 135 x 200 cm. " * 3,
        [("Which duvet size fits a 140 x 200 cm bed?", "A 135 x 200 cm duvet is the usual choice.")]),
    art(3, "how-to-buy-a-mattress", "matratze-kaufen", "Buying a Mattress: What Really Matters",
        "Firmness, material, size and build quality are the criteria that matter when you buy a mattress. " * 4,
        [("How long does a mattress last?", "That depends on the material and care.")]),
    art(4, "bamboo-tencel-down-pillow", "bambus-tencel-daune", "Bamboo, Tencel or Down Pillows", "Down pillow text. " * 20,
        [("Are down pillows warm?", "Yes.")], aid=HELD_ID),
    art(5, "muslin-bedding-trend", "musselin-bettdecke", "Muslin Bedding: What's Behind the Trend?",
        "Muslin is a loosely woven cotton that softens with every wash and feels airy. " * 4,
        [("Does muslin bedding shrink?", "It can shrink a little in the first wash, so allow for it.")]),
    art(6, "best-throw-blanket-material", "decke-material-vergleich", "Throw Blankets in Wool, Cashmere or Cotton",
        "Wool is warm and durable, cashmere is soft and light, cotton is easy to wash. " * 4,
        [("Which throw blanket material is easiest to care for?", "Cotton is the easiest to wash.")]),
    art(7, "washing-new-bed-linen", "neue-bettwaesche-waschen", "Washing New Bed Linen: Why the First Wash Matters",
        "Wash new bed linen once before first use to remove finishing agents. " * 4,
        [("Should I wash new sheets before using them?", "Yes, once at the temperature on the care label.")]),
]


def gsc_row(query, page, impressions, position=8.0, clicks=0):
    return {"query": query, "impressions": impressions, "clicks": clicks, "ctr": clicks / impressions, "position": position, "page": page}


def claude_json(question="Q"):
    body = ("Short answer: it depends on the weight of the fabric. " * 6 + "\n\nA typical flannel duvet cover weighs about "
            "180 g/m2 and a 135 x 200 cm duvet is the usual choice for one person.\n\n[[LINK]]")
    return json.dumps({"title": question.rstrip("?") + "?", "body": body, "unsupported": []})


PASTED = "Why does new bed linen need a first wash before use? | Which throw blanket material is best for winter?"


class QuestionHelpers(unittest.TestCase):
    def test_question_detection_and_language(self):
        for q in ("wie oft bettwäsche waschen", "how often to wash sheets", "Was ist Percale", "welche decke im winter",
                  "bettwäsche waschen?", "which duvet is warmest", "warum riecht neue bettwäsche"):
            self.assertTrue(questions.is_question(q), q)
        for q in ("bettwäsche 135x200", "bamboo bedding", "flanell bettwäsche kaufen"):
            self.assertFalse(questions.is_question(q), q)
        self.assertEqual([questions.language(q) for q in ("wie oft bettwäsche waschen", "how often to wash sheets",
                                                          "welche decke im winter", "is tencel good for summer")],
                         ["de", "en", "de", "en"])

    def test_dedupe_hash_ignores_case_punctuation_and_trailing_question_mark(self):
        self.assertEqual(questions.qhash("Wie oft Bettwäsche waschen?"), questions.qhash("wie oft  bettwäsche waschen"))
        self.assertNotEqual(questions.qhash("wie oft bettwäsche waschen"), questions.qhash("wie oft decke waschen"))
        self.assertEqual(len(questions.qhash("x")), 16)

    def test_pasted_list_is_split_even_if_newlines_collapse(self):
        got = questions.parse_pasted("1. How often should I wash sheets? What is percale? | Why does my duvet clump;\n- Welche Bettwäsche im Sommer?\nxx")
        self.assertEqual(got, ["How often should I wash sheets?", "What is percale?", "Why does my duvet clump",
                               "Welche Bettwäsche im Sommer?"])
        self.assertEqual(questions.parse_pasted(""), [])
        self.assertEqual(len(questions.parse_pasted("What is percale?\nwhat is percale")), 1)


class SearchConsole(unittest.TestCase):
    def test_window_matches_the_weekly_seo_report(self):
        start, end = gsc.window(date(2026, 11, 2))
        self.assertEqual((start, end), (date(2026, 10, 24), date(2026, 10, 30)))
        self.assertEqual((end - start).days, 6)
        self.assertEqual(end, date(2026, 11, 2) - timedelta(days=3))

    def test_credentials_raw_base64_and_missing(self):
        info = {"client_email": "x@y", "private_key": "k"}
        for raw in (json.dumps(info), base64.b64encode(json.dumps(info).encode()).decode()):
            with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": raw}):
                self.assertEqual(gsc.credentials_info(), info)
        with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            with self.assertRaises(gsc.GscError):
                gsc.credentials_info()
        with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": "not json or base64!!"}):
            with self.assertRaises(gsc.GscError):
                gsc.credentials_info()

    def test_aggregation_and_filter(self):
        rows = [{"keys": ["wie oft bettwäsche waschen", "https://x/blogs/blog/a"], "clicks": 0, "impressions": 40, "position": 8.0},
                {"keys": ["wie oft bettwäsche waschen", "https://x/blogs/blog/b"], "clicks": 1, "impressions": 10, "position": 12.0},
                {"keys": ["bettwäsche kaufen", "https://x/c"], "clicks": 0, "impressions": 99, "position": 3.0},      # not a question
                {"keys": ["what is percale", "https://x/p"], "clicks": 5, "impressions": 50, "position": 2.0},       # good CTR + position
                {"keys": ["how to wash a duvet", "https://x/d"], "clicks": 0, "impressions": 2, "position": 9.0},    # too few impressions
                {"keys": ["why is my duvet flat", "https://x/e"], "clicks": 0, "impressions": 30, "position": 40.0},  # low CTR, far down
                {"keys": ["which pillow is best", "https://x/f"], "clicks": 20, "impressions": 100, "position": 6.0}]  # pos 5-20 (ctr high)
        agg = gsc.aggregate(rows)
        top = {a["query"]: a for a in agg}["wie oft bettwäsche waschen"]
        self.assertEqual((top["impressions"], top["clicks"], top["page"]), (50, 1, "https://x/blogs/blog/a"))
        self.assertAlmostEqual(top["position"], (8 * 40 + 12 * 10) / 50)
        got = [a["query"] for a in gsc.question_queries(agg, 3)]
        self.assertEqual(got, ["which pillow is best", "wie oft bettwäsche waschen", "why is my duvet flat"])

    def test_pagination_and_no_query_text_is_printed(self):
        pages = [[{"keys": [f"how q{i}", "p"], "impressions": 5, "clicks": 0, "position": 9} for i in range(3)],
                 [{"keys": ["how last", "p"], "impressions": 5, "clicks": 0, "position": 9}]]
        calls = []

        def fake(method, url, label, **kw):
            calls.append(kw["json"]["startRow"])
            return mock.Mock(json=lambda: {"rows": pages[len(calls) - 1]})
        out = io.StringIO()
        with mock.patch.object(gsc, "ROW_LIMIT", 3), mock.patch.object(http, "request", fake), contextlib.redirect_stdout(out):
            rows = gsc.fetch_rows("tok", date(2026, 10, 1), date(2026, 10, 7))
        self.assertEqual((len(rows), calls), (4, [0, 3]))
        self.assertNotIn("how last", out.getvalue())


class Articles(unittest.TestCase):
    def test_faq_from_json_ld_and_accordion_fallback(self):
        pairs = ra.faq_pairs(INDEX[0]["body"])
        self.assertEqual(pairs[0][0], "Is flannel warmer than flannelette?")
        accordion = ('<details><summary><div class="t"><span>Which size?</span><span>x</span></div></summary>'
                     '<div class="accordion__content"><div class="prose">The 135 one.</div></div></details>')
        self.assertEqual(ra.faq_pairs(accordion), [("Which size?", "The 135 one.")])
        self.assertEqual(ra.faq_pairs("<p>no faq</p>"), [])

    def test_source_text_has_no_markup_placeholders_or_script(self):
        t = ra.source_text({"body": "<p>Hello <b>world</b>.</p><!-- PHOTO PLACEHOLDER x -->[[mdc-products]]" + faq_json([("Q1?", "A1.")])})
        self.assertIn("Hello world.", t)
        self.assertNotIn("<", t)
        self.assertNotIn("PLACEHOLDER", t)
        self.assertNotIn("mdc-products", t)
        self.assertNotIn("@context", t)
        self.assertIn("Q: Q1? A: A1.", t)

    def test_page_matching_by_english_and_german_handle_and_locale_paths(self):
        for url in ("https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber",
                    "https://www.maisondecocon.com/en/blogs/blog/flannel-vs-flannelette-bedding?x=1",
                    "https://www.maisondecocon.com/de/blogs/blog/Winterbettwaesche-Flanell-Biber"):
            self.assertEqual(ra.match_page(url, INDEX)["id"], INDEX[0]["id"], url)
        self.assertIsNone(ra.match_page("https://www.maisondecocon.com/collections/beds", INDEX))
        self.assertIsNone(ra.match_page("https://www.maisondecocon.com/blogs/blog/unknown-handle", INDEX))

    def test_text_matching_needs_two_distinctive_tokens(self):
        self.assertEqual(ra.match_text("which duvet size for a double bed", INDEX)["id"], INDEX[1]["id"])
        self.assertEqual(ra.match_text("what really matters when buying a mattress", INDEX)["id"], INDEX[2]["id"])
        self.assertIsNone(ra.match_text("how long does a mattress last", INDEX))   # only "mattress" is distinctive
        self.assertIsNone(ra.match_text("what is the weather", INDEX))
        self.assertIsNone(ra.match_text("duvet", INDEX))                      # one token is not enough

    def test_held_and_unpublished_are_not_eligible(self):
        ids = {a["id"] for a in ra.eligible(INDEX)}
        self.assertNotIn(HELD_ID, ids)
        draft_art = {**INDEX[0], "id": "123", "published": False}
        self.assertNotIn("123", {a["id"] for a in ra.eligible(INDEX + [draft_art])})

    def test_links_follow_the_question_language_with_utm(self):
        env = {"STOREFRONT_DOMAIN": "www.maisondecocon.com", "SHOPIFY_BLOG_HANDLE": "blog"}
        with mock.patch.dict(os.environ, env):
            a = INDEX[0]
            utm = ra.reddit_utm(a)
            self.assertEqual(ra.article_url(a, "en", utm),
                             "https://www.maisondecocon.com/en/blogs/blog/flannel-vs-flannelette-bedding"
                             "?utm_source=reddit&utm_medium=social&utm_campaign=flannel-vs-flannelette-bedding")
            self.assertEqual(ra.article_url(a, "de", utm),
                             "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber"
                             "?utm_source=reddit&utm_medium=social&utm_campaign=flannel-vs-flannelette-bedding")
            self.assertTrue(ra.article_url({**a, "de_handle": None}, "de", utm).startswith("https://www.maisondecocon.com/en/blogs/"))


class RedditCopy(unittest.TestCase):
    LINK = "https://www.maisondecocon.com/en/blogs/blog/x?utm_source=reddit&utm_medium=social&utm_campaign=x"
    SRC = "Flannel is brushed on both sides. A typical duvet cover weighs about 180 g/m2."

    def raw(self, body, **kw):
        return {"title": "What is flannel?", "body": body, "unsupported": [], **kw}

    def test_exactly_one_disclosure_and_at_most_one_link_ours(self):
        for lang, line in (("en", "Disclosure: I work for Maison de Cocon"), ("de", "Offenlegung: Ich arbeite für Maison de Cocon")):
            d = reddit_copy.finalize(self.raw("Answer.\n\n[[LINK]]"), lang, self.LINK)
            self.assertEqual(d["body"].count(line), 1)
            self.assertEqual(d["body"].count("http"), 1)
            self.assertIn(self.LINK, d["body"])
            self.assertTrue(d["body"].rstrip().endswith("."))
            self.assertNotIn("[[LINK]]", d["body"])

    def test_model_written_urls_are_removed_and_flagged(self):
        d = reddit_copy.finalize(self.raw("See https://evil.example/x and www.maisondecocon.com.\n\n[[LINK]]"), "en", self.LINK)
        self.assertNotIn("evil.example", d["body"])
        self.assertEqual(d["body"].count("http"), 1)
        self.assertTrue(d["link_dropped"])
        self.assertTrue(any("own URL" in f for f in reddit_copy.flags_for({**d, "body": d["body"] * 2}, self.SRC)))

    def test_no_link_means_none_in_the_post(self):
        d = reddit_copy.finalize(self.raw("Answer."), "en", None)
        self.assertNotIn("http", d["body"])

    def test_flags_cover_numbers_claims_and_missing_source(self):
        long_body = "Flannel is brushed. " * 30
        ok = reddit_copy.flags_for({"body": long_body + "It weighs about 180 g/m2.", "unsupported": []}, self.SRC)
        self.assertEqual(ok, [])
        f = reddit_copy.flags_for({"body": long_body + "It weighs 250 g/m2 and is hypoallergenic and OEKO-TEX certified.",
                                   "unsupported": ["it is hypoallergenic"]}, self.SRC)
        text = " | ".join(f)
        self.assertIn("250", text)
        self.assertIn("hypoallergen", text)
        self.assertIn("oeko-tex", text)
        self.assertIn("not in the article", text)
        self.assertTrue(any("NO MATCHING ARTICLE" in x for x in reddit_copy.flags_for({"body": long_body, "unsupported": []}, "", False)))
        self.assertTrue(any("Short answer" in x for x in reddit_copy.flags_for({"body": "tiny", "unsupported": []}, self.SRC)))
        self.assertEqual(reddit_copy.flags_for({"body": long_body + "wir verkaufen", "unsupported": []}, self.SRC and "wir verkaufen das"), [])

    def test_prompt_states_the_rules(self):
        s = reddit_copy._system("en")
        for needle in ("ONLY facts", "no sales tone", "Do NOT write any URL", "[[LINK]]", "English", "unsupported"):
            self.assertIn(needle, s)
        self.assertIn("German", reddit_copy._system("de"))
        seen = {}
        with mock.patch.object(copy, "_call_claude", side_effect=lambda s, u, m, max_tokens=0: seen.update(u=u) or claude_json()):
            reddit_copy.generate("Is flannel warm?", "en", "SOURCE-FACTS-HERE", self.LINK, "m")
        self.assertIn("SOURCE-FACTS-HERE", seen["u"])


class Selection(unittest.TestCase):
    def c(self, text, source, aid, score=1):
        return {"text": text, "lang": "en", "source": source, "article_id": aid, "signals": {}, "score": score}

    def test_slots_gsc_then_pasted_then_faq_with_distinct_articles(self):
        g = [self.c(f"How gsc {i}?", "gsc", str(i)) for i in range(1, 6)]
        p = [self.c(f"What pasted {i}?", "findquestions", str(10 + i)) for i in range(1, 5)]
        f = [self.c(f"Which faq {i}?", "faq", str(20 + i)) for i in range(1, 5)]
        got = reddit_draft.select(g, p, f, set())
        self.assertEqual([c["source"] for c in got], ["gsc"] * 3 + ["findquestions"] * 2)
        self.assertEqual(len({c["article_id"] for c in got}), 5)

    def test_shortfalls_are_filled_gsc_then_pasted_then_faq(self):
        g = [self.c("How gsc 1?", "gsc", "1")]
        p = [self.c("What pasted 1?", "findquestions", "11")]
        f = [self.c(f"Which faq {i}?", "faq", str(20 + i)) for i in range(1, 6)]
        got = reddit_draft.select(g, p, f, set())
        self.assertEqual([c["source"] for c in got], ["gsc", "findquestions", "faq", "faq", "faq"])

    def test_one_question_per_article_and_ledger_dedupe(self):
        g = [self.c("How flannel one?", "gsc", "1"), self.c("How flannel two?", "gsc", "1"), self.c("How duvet?", "gsc", "2")]
        seen = {questions.qhash("How duvet?")}
        got = reddit_draft.select(g, [], [], seen)
        self.assertEqual([c["text"] for c in got], ["How flannel one?"])

    def test_gsc_rows_best_per_article_held_and_unmatched_are_not_used(self):
        rows = [gsc_row("was ist besser flanell oder biber", "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber", 90),
                gsc_row("ist flanell das gleiche wie biber", "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber", 70),
                gsc_row("are down pillows warm", "https://www.maisondecocon.com/en/blogs/blog/bamboo-tencel-down-pillow", 60),
                gsc_row("what is the weather", "https://www.maisondecocon.com/collections/x", 50)]
        cands, stats = reddit_draft.from_gsc(rows, INDEX, config.HOLD_ARTICLE_IDS)
        self.assertEqual([(c["text"], c["lang"], c["source"]) for c in cands], [("Was ist besser flanell oder biber?", "de", "gsc")])
        self.assertEqual((stats["held"], stats["unmatched"]), (1, 1))

    def test_faq_candidates_are_one_per_article_and_never_from_held(self):
        cands = reddit_draft.from_faq(INDEX, config.HOLD_ARTICLE_IDS, "20261102")
        ids = [c["article_id"] for c in cands]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(HELD_ID, ids)
        self.assertEqual([c["article_id"] for c in cands], [c["article_id"] for c in reddit_draft.from_faq(INDEX, config.HOLD_ARTICLE_IDS, "20261102")])


class WeeklyBase(tp.InTmp):
    """Shared fixture: a faked workflow environment (artifacts = files in ./uploaded)."""

    def setUp(self):
        super().setUp()
        from cryptography.fernet import Fernet
        self.env = mock.patch.dict(os.environ, {
            **{k: "x" * 12 for k in reddit_draft.BASE_ENV}, "REDDIT_ARTIFACT_KEY": Fernet.generate_key().decode(),
            "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main", "SHOPIFY_BLOG_HANDLE": "blog",
            "STOREFRONT_DOMAIN": "www.maisondecocon.com", "RESEND_FROM": "a@b.c", "RESEND_TO": "d@e.f", "QUESTIONS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sent, self.uploaded = [], Path("uploaded")
        self.uploaded.mkdir()
        self.gsc_candidates = [gsc_row("was ist besser flanell oder biber", "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber", 90),
                               gsc_row("which duvet size fits a double bed", "https://www.maisondecocon.com/en/blogs/blog/duvet-sizes-for-every-bed", 70),
                               gsc_row("how to choose a mattress", "https://www.maisondecocon.com/en/blogs/blog/how-to-buy-a-mattress", 60)]

        def fake_read(name, filename):  # what the workflow uploaded, as the next run/approval would download it
            f = self.uploaded / f"{name}.enc"
            return reddit_store.decrypt(f.read_bytes()) if f.exists() else None
        patches = [mock.patch.object(reddit_store, "read", fake_read),
                   mock.patch.object(approval, "_send", side_effect=lambda s, b: self.sent.append((s, b))),
                   mock.patch.object(ra, "fetch_index", return_value=INDEX),
                   mock.patch.object(copy, "_call_claude", side_effect=lambda s, u, m, max_tokens=0: claude_json(u.split("\n")[0][len("Question: "):]))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_week(self, today, pasted="", gsc_ok=True):
        pull = {"rows": 100, "queries": 90, "candidates": self.gsc_candidates, "window": (today, today)}
        out = io.StringIO()
        before = len(self.sent)
        with mock.patch.dict(os.environ, {"QUESTIONS": pasted}), \
                mock.patch.object(gsc, "pull", return_value=pull) if gsc_ok else mock.patch.object(gsc, "pull", side_effect=gsc.GscError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")), \
                contextlib.redirect_stdout(out):
            try:
                reddit_draft.main(today)
            except SystemExit:
                pass
        batch = today.strftime("%Y%m%d")
        for name, src in ((f"reddit-batch-{batch}", "reddit-out/batch.enc"), ("reddit-pool", "reddit-out/pool.enc")):
            if Path(src).exists():
                shutil.copy(src, self.uploaded / f"{name}.enc")
        return self.sent[before:], out.getvalue()


class WeeklyRun(WeeklyBase):
    def test_monday_run_sends_five_draft_emails(self):
        sent, log = self.run_week(date(2026, 11, 2), PASTED)
        drafts = [(s, b) for s, b in sent if s.startswith("Reddit draft")]
        self.assertEqual(len(drafts), 5)
        data = ledger.load()
        ids = sorted(k for k in data)
        self.assertEqual(ids, [f"2026110 2".replace(" ", "") + str(n) for n in range(1, 6)] if False else [f"20261102{n}" for n in range(1, 6)])
        for aid in ids:
            e = data[aid]["reddit"]
            self.assertEqual(set(e), {"status", "qhash", "batch", "timestamp"})   # nothing but status + hash
            self.assertEqual((e["status"], e["batch"]), ("draft", "20261102"))
        for (subject, body), aid in zip(drafts, ids):
            self.assertEqual(body.count("<a href"), 2)                                 # Approve / Reject only
            self.assertIn(f"title=approve%3A{aid}%3Areddit", body)
            self.assertIn(f"title=reject%3A{aid}%3Areddit", body)
        text = html.unescape(" ".join(b for _, b in drafts))
        self.assertIn("Disclosure: I work for Maison de Cocon", text)
        self.assertIn("Offenlegung: Ich arbeite für Maison de Cocon", text)         # the German question is answered in German
        self.assertIn("utm_source=reddit&utm_medium=social&utm_campaign=", text)
        self.assertEqual(text.count("Search Console (last 7 days)"), 3)               # 3 from GSC
        self.assertEqual(text.count("findquestions list pasted by you"), 2)          # 2 pasted
        self.assertIn("findquestions list pasted by you", text)
        self.assertIn("Check before approving", text)                                # flags are shown prominently

    def test_nothing_private_is_committed_logged_or_stored_in_plaintext(self):
        sent, log = self.run_week(date(2026, 11, 2), PASTED)
        secrets_ = ["flanell oder biber", "duvet size fits a double bed", "first wash before use", "throw blanket material is best", "Short answer"]
        ledger_text = Path("state/ledger.json").read_text(encoding="utf-8")
        for s in secrets_:
            self.assertNotIn(s, ledger_text)
            self.assertNotIn(s.lower(), log.lower())
        for f in Path("reddit-out").glob("*.enc"):
            raw = f.read_bytes()
            for s in secrets_:
                self.assertNotIn(s.encode(), raw)                                    # encrypted at rest
        self.assertTrue(list(Path("reddit-out").glob("batch.enc")))
        self.assertFalse(list(Path("state").rglob("*reddit*")) )                     # no draft/inbox file in state/
        gi = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("reddit-out/", gi)
        self.assertIn("reddit/", gi)

    def test_decrypts_only_with_the_right_key(self):
        self.run_week(date(2026, 11, 2))
        blob = Path("reddit-out/batch.enc").read_bytes()
        self.assertEqual(len(reddit_store.decrypt(blob)["drafts"]), 5)
        from cryptography.fernet import Fernet
        with mock.patch.dict(os.environ, {"REDDIT_ARTIFACT_KEY": Fernet.generate_key().decode()}):
            with self.assertRaises(reddit_store.StoreError):
                reddit_store.decrypt(blob)

    def test_next_weeks_run_skips_questions_already_used(self):
        pasted = PASTED
        self.run_week(date(2026, 11, 2), pasted)
        first = {d["question"] for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"]}
        sent, _ = self.run_week(date(2026, 11, 9), pasted)                           # same inputs a week later
        second = {d["question"] for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"]}
        self.assertTrue(second)
        self.assertEqual(first & second, set())                                      # nothing is drafted twice
        self.assertEqual({q for q in ledger.load() if q.startswith("20261109")} & {q for q in ledger.load() if q.startswith("20261102")}, set())
        hashes = [e["reddit"]["qhash"] for e in ledger.load().values()]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_unused_pasted_questions_are_carried_over_in_the_encrypted_pool(self):
        pasted = PASTED + " | What is the story behind the muslin bedding trend?"      # a third match: one slot too many
        self.run_week(date(2026, 11, 2), pasted)
        pool = reddit_store.decrypt(Path("reddit-out/pool.enc").read_bytes())
        self.assertGreaterEqual(len(pool["questions"]), 1)
        sent, _ = self.run_week(date(2026, 11, 9), "")                               # nothing pasted this week: pool is used
        self.assertTrue(any("findquestions list pasted by you" in html.unescape(b) for _, b in sent))

    def test_search_console_failure_is_reported_and_never_replaced_by_old_data(self):
        sent, log = self.run_week(date(2026, 11, 2), gsc_ok=False)
        self.assertTrue(any("Search Console unavailable" in s for s, _ in sent))
        drafts = [b for s, b in sent if s.startswith("Reddit draft")]
        self.assertTrue(drafts)                                                       # FAQ source still produces drafts
        self.assertNotIn("Search Console (last 7 days)", html.unescape(" ".join(drafts)))

    def test_held_article_is_never_a_link_target_or_source(self):
        self.gsc_candidates = [gsc_row("are down pillows warm", "https://www.maisondecocon.com/en/blogs/blog/bamboo-tencel-down-pillow", 99)]
        sent, _ = self.run_week(date(2026, 11, 2), "Which down pillow is warm? | Is a bamboo down pillow good?")
        text = html.unescape(" ".join(b for s, b in sent if s.startswith("Reddit draft")))
        self.assertNotIn("bamboo-tencel-down-pillow", text)
        self.assertNotIn("Bamboo, Tencel or Down Pillows", text)


class ManualPublishing(WeeklyBase):
    def setUp(self):
        super().setUp()
        self.run_week(date(2026, 11, 2))
        self.ids = sorted(ledger.load())
        self.base_env = {**tp.MainFlow.env, "RESULT_FILE": "result.md", "GITHUB_TOKEN": "gh-token-xxxx", "REDDIT_URL": ""}

    def act(self, title, url="", today=date(2026, 11, 3)):
        env = {**self.base_env, "TITLE": title, "REDDIT_URL": url}
        before = len(self.sent)
        with mock.patch.dict(os.environ, env), mock.patch.object(publish, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(today.year, today.month, today.day, 9, 0, tzinfo=__import__("datetime").timezone.utc)
            try:
                publish.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, self.sent[before:], Path("result.md").read_text(encoding="utf-8")

    def test_approve_sends_the_copy_ready_email_with_a_suggested_day(self):
        qid = self.ids[0]
        code, sent, result = self.act(f"approve:{qid}:reddit")
        self.assertEqual(code, 0)
        e = ledger.load()[qid]["reddit"]
        self.assertEqual((e["status"], e["post_on"]), ("approved", "2026-11-04"))     # tomorrow
        subject, body = sent[-1]
        self.assertTrue(subject.startswith("Ready to post (Wednesday 2026-11-04)"))
        draft = next(d for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"] if d["id"] == qid)
        text = html.unescape(body)
        self.assertIn(draft["title"], text)
        self.assertIn(draft["body"].split("\n")[0], text)
        self.assertIn("Wednesday 2026-11-04", text)
        self.assertIn(f"<code>{qid}</code>", body)
        self.assertIn("channel</code> = <code>reddit", body)
        self.assertIn("/actions/workflows/social-publish.yml", body)
        # the public issue comment / result file holds no draft text
        self.assertNotIn(draft["title"], result)
        self.assertNotIn(draft["body"][:40], result)
        self.assertIn("approved", result)

    def test_one_post_per_day_suggestions(self):
        days = []
        for qid in self.ids[:3]:
            self.act(f"approve:{qid}:reddit")
            days.append(ledger.load()[qid]["reddit"]["post_on"])
        self.assertEqual(days, ["2026-11-04", "2026-11-05", "2026-11-06"])

    def test_approving_twice_does_nothing(self):
        qid = self.ids[0]
        self.act(f"approve:{qid}:reddit")
        n, before = len(self.sent), ledger.load()
        code, sent, result = self.act(f"approve:{qid}:reddit")
        self.assertEqual((code, len(self.sent), ledger.load()), (0, n, before))
        self.assertIn("already approved", result)

    def test_submitting_the_url_marks_it_published(self):
        qid = self.ids[0]
        self.act(f"approve:{qid}:reddit")
        env = {k: v for k, v in self.base_env.items() if k not in ("GITHUB_TOKEN",)}
        url = "https://old.reddit.com/r/MaisonDeCocon/comments/1ab2cd/how_do_i_wash_a_muslin/?utm_source=share#c"
        code, sent, result = self.act(f"approve:{qid}:reddit", url=url, today=date(2026, 11, 4))
        e = ledger.load()[qid]["reddit"]
        self.assertEqual(code, 0)
        self.assertEqual((e["status"], e["post_id"], e["url"], e["published_on"]),
                         ("published", "1ab2cd", "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/", ledger.now()[:10]))
        self.assertIn("recorded from the URL you gave", result)
        self.assertEqual(set(e), {"status", "qhash", "batch", "timestamp", "post_on", "post_id", "url", "published_on"})
        code, _, result = self.act(f"approve:{qid}:reddit", url=url)                  # re-submit: no-op
        self.assertEqual(code, 0)
        self.assertIn("already published", result)

    def test_url_must_be_a_post_in_our_subreddit(self):
        qid = self.ids[0]
        self.act(f"approve:{qid}:reddit")
        for bad in ("https://www.reddit.com/r/bedding/comments/abc123/x/", "https://redd.it/abc123", "http://www.reddit.com/r/MaisonDeCocon/comments/abc123/",
                    "https://evil.example/r/MaisonDeCocon/comments/abc123/", "https://www.reddit.com/r/MaisonDeCocon/", "javascript:alert(1)",
                    "https://www.reddit.com/r/MaisonDeCocon/comments/abc123/x/ https://evil"):
            code, _, result = self.act(f"approve:{qid}:reddit", url=bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(ledger.load()[qid]["reddit"]["status"], "approved", bad)
        self.assertIsNone(publish.reddit_post_id("https://www.reddit.com/r/maisoncocon2/comments/abc123/"))
        self.assertEqual(publish.reddit_post_id("https://www.reddit.com/r/maisondecocon/comments/ABC123/Title"), "abc123")

    def test_url_cannot_be_submitted_before_approval(self):
        qid = self.ids[1]
        code, _, result = self.act(f"approve:{qid}:reddit", url="https://www.reddit.com/r/MaisonDeCocon/comments/abc123/")
        self.assertEqual((code, ledger.load()[qid]["reddit"]["status"]), (1, "draft"))
        self.assertIn("approve it first", result)

    def test_reject_works_for_drafts_and_approved(self):
        a, b = self.ids[0], self.ids[1]
        self.act(f"approve:{a}:reddit")
        self.act(f"reject:{a}:reddit")
        self.act(f"reject:{b}:reddit")
        self.assertEqual([ledger.load()[x]["reddit"]["status"] for x in (a, b)], ["rejected", "rejected"])
        code, _, result = self.act(f"approve:{a}:reddit")
        self.assertEqual(code, 1)

    def test_expired_or_missing_artifact_blocks_approval_clearly(self):
        qid = self.ids[0]
        (self.uploaded / "reddit-batch-20261102.enc").unlink()
        code, sent, result = self.act(f"approve:{qid}:reddit")
        self.assertEqual((code, ledger.load()[qid]["reddit"]["status"]), (1, "draft"))
        self.assertIn("90-day", result)
        self.assertTrue(any(s.startswith("[FAILED]") for s, _ in sent))

    def test_unknown_id_and_title_rules(self):
        code, _, result = self.act("approve:202611029:reddit")
        self.assertEqual(code, 1)
        self.assertEqual(publish.parse_title("approve:202611021:reddit"), ("approve", "202611021", ["reddit"]))
        with self.assertRaises(ValueError):
            publish.parse_title("approve:flannel-vs-flannelette:reddit")


class Workflows(unittest.TestCase):
    def load(self, name):
        return yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / name).read_text(encoding="utf-8"))

    def test_reddit_draft_workflow_is_weekly_monday_with_a_questions_input_and_90_day_encrypted_artifacts(self):
        wf = self.load("social-reddit-draft.yml")
        on = wf.get(True) or wf.get("on")
        self.assertEqual(on["schedule"][0]["cron"].split()[4], "1")                  # Monday
        self.assertIn("questions", on["workflow_dispatch"]["inputs"])
        steps = wf["jobs"]["draft"]["steps"]
        uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
        self.assertEqual({u["with"]["path"] for u in uploads}, {"reddit-out/batch.enc", "reddit-out/pool.enc"})
        self.assertTrue(all(u["with"]["retention-days"] == 90 for u in uploads))
        run = next(s for s in steps if s.get("id") == "draft")
        self.assertEqual(run["env"]["QUESTIONS"], "${{ inputs.questions }}")        # via env, never in a shell line
        self.assertNotIn("inputs.", run["run"])
        self.assertEqual(wf["permissions"]["actions"], "read")

    def test_publish_workflow_takes_the_reddit_url_via_env(self):
        wf = self.load("social-publish.yml")
        on = wf.get(True) or wf.get("on")
        self.assertIn("url", on["workflow_dispatch"]["inputs"])
        self.assertIn("reddit", on["workflow_dispatch"]["inputs"]["channel"]["options"])
        step = next(s for s in wf["jobs"]["publish"]["steps"] if s.get("id") == "run")
        self.assertEqual(step["env"]["REDDIT_URL"], "${{ inputs.url }}")
        self.assertNotIn("inputs.", step["run"])
        self.assertEqual(wf["permissions"]["actions"], "read")


if __name__ == "__main__":
    unittest.main()
