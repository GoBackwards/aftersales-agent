from __future__ import annotations

import hashlib
import unittest

from support_agent.public_policy_collection import (
    PublicPolicyCollectionError, extract_policy_text, make_chunks,
)


TITLE = "测试售后政策"


def article(body="<p>1、退货说明</p><p>商品符合完好要求时可以申请，特殊商品以页面标识为准。</p>", title=TITLE):
    return f'<html><head><meta charset="utf-8"></head><body>导航栏<div id="pdfContainer"><div class="contxt"><div class="help-tit1 flk06">{title}</div>{body}</div></div><footer>投票推荐</footer></body></html>'


class PublicPolicyCollectionTests(unittest.TestCase):
    def test_extract_excludes_navigation_scripts_hidden_and_footer(self):
        body = '<p>有效规则：商品符合要求可以申请，条件和例外均须核对。</p><script>secret_token</script><p hidden>hidden_secret</p><span style="display:none">copy_blob</span>'
        text, images = extract_policy_text(article(body), TITLE)
        for omitted in ["导航栏", "投票推荐", "secret_token", "hidden_secret", "copy_blob"]:
            self.assertNotIn(omitted, text)
        self.assertIn("有效规则", text)
        self.assertEqual(images, [])

    def test_extract_preserves_table_row_and_column_boundaries(self):
        body = '<p>售后运费规则须同时核对客户原因及会员类型。</p><table><tr><th>原因</th><th>普通会员</th><th>PLUS会员</th></tr><tr><td>客户原因</td><td>客户承担</td><td>京东承担</td></tr></table><p>例外：实际权益另有说明时应核对权益条件。</p>'
        text, _ = extract_policy_text(article(body), TITLE)
        self.assertIn("原因 | 普通会员 | PLUS会员\n客户原因 | 客户承担 | 京东承担", text)
        self.assertIn("例外：实际权益", text)

    def test_extract_images_are_omissions_without_synthesized_policy(self):
        text, images = extract_policy_text(article('<p>商品是否支持须核对详情页标识及商品完好条件。</p><img src="//img.example/chart.png" alt="分类图"/>'), TITLE)
        self.assertEqual(images, [{"sequence": 1, "source_url": "//img.example/chart.png", "alt": "分类图"}])
        self.assertNotIn("分类图", text)
        self.assertNotIn("chart.png", text)

    def test_extract_layout_title_and_image_only_fail_closed(self):
        values = [(article().replace('class="contxt"', 'class="changed"'), "ARTICLE_CONTAINER"), (article(title="另一政策"), "ARTICLE_TITLE"), (article('<img src="a.png">'), "ARTICLE_TEXT_MISSING"), (article() + article(), "ARTICLE_CONTAINER_AMBIGUOUS")]
        for html, code in values:
            with self.subTest(code=code), self.assertRaisesRegex(PublicPolicyCollectionError, code):
                extract_policy_text(html, TITLE)

    def test_chunk_offsets_hashes_and_oversized_section_preserved(self):
        long_section = "注意事项：\n\n" + "必要条件与例外必须完整保留。" * 120
        text = "标题\n\n1、一般规则\n\n可以申请但须同时满足条件。\n\n" + long_section + "\n"
        chunks = make_chunks(text, "jd-test", max_characters=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(len(chunk["content"]) > 1200 for chunk in chunks))
        self.assertTrue(any(long_section in chunk["content"] for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(chunk["content"], text[chunk["start"]:chunk["end"]])
            self.assertEqual(chunk["sha256"], hashlib.sha256(chunk["content"].encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
