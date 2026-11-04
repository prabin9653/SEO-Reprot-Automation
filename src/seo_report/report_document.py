"""The complete report document: cover, keyword pages and the report sections (R5).

`plan_report` plans every page first (keyword pages, then Search Console, geography, GA4 and the recommendations with
the contact block), raising `KeywordPaginationError` or `ContentPaginationError` if any content cannot be shown, so a
failure produces no document. `render_report_document` then renders the pages with the R3 shell.

Order and numbering: page 1 is the cover; the keyword pages follow (2, 3, ...); then the section pages continue the
numbering with no gap and no repeat. The page count is whatever the content needs (nothing forces a fixed number of
pages). HTML only: no browser, PDF or finalization step is involved.
"""

from dataclasses import dataclass

from markupsafe import Markup

from seo_report import keyword_pages as kp
from seo_report import render_input as ri
from seo_report import report_sections as rs
from seo_report import report_templates as rt


@dataclass(frozen=True)
class ReportPlan:
    keyword_pages: tuple[kp.KeywordPage, ...]
    section_pages: tuple[rs.SectionPage, ...]

    @property
    def page_count(self) -> int:
        return 1 + len(self.keyword_pages) + len(self.section_pages)


def plan_report(render_input: ri.ClientRenderInput, assets: "rt.TemplateAssets") -> ReportPlan:
    """Plan every page. Raises before any HTML is produced when something cannot be placed."""
    keyword_pages = kp.plan_keyword_pages(render_input, assets)
    section_pages = rs.plan_section_pages(render_input, assets)
    return ReportPlan(keyword_pages, section_pages)


def render_report_document(render_input: ri.ClientRenderInput, assets: "rt.TemplateAssets") -> str:
    """The whole report as one HTML document."""
    plan = plan_report(render_input, assets)
    pages = [rt.cover_page_spec(render_input)]
    keywords_body = rt.get_template("keywords.html").module.keyword_page_body
    for page in plan.keyword_pages:
        pages.append({"kind": "keywords", "number": 1 + page.number, "body": keywords_body(page, kp.COLUMNS)})
    offset = 1 + len(plan.keyword_pages)
    for page in plan.section_pages:
        pages.append({"kind": "report", "number": offset + page.number,
                      "body": rs.render_section_page_body(page, assets)})
    return rt.render_document(render_input, assets, pages)
