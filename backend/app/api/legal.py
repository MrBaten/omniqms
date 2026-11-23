"""Public legal pages Meta and Google ask for: privacy policy, data deletion instructions, terms."""
from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import get_settings

router = APIRouter(tags=["legal"], include_in_schema=False)

_STYLE = ("body{font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;max-width:720px;margin:40px auto;"
          "padding:0 20px;color:#152030}h1{font-size:26px}h2{font-size:18px;margin-top:28px}"
          "code{background:#f1f3f6;padding:1px 5px;border-radius:4px}")


def _page(title: str, body: str) -> HTMLResponse:
    s = get_settings()
    who = escape(s.operator_name or "the operator of this OmniQMS installation")
    contact = (f'<a href="mailto:{escape(s.contact_email)}">{escape(s.contact_email)}</a>'
               if s.contact_email else "the contact address listed on the operator's website")
    html = (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{escape(title)}</title><style>{_STYLE}</style>"
            f"</head><body><h1>{escape(title)}</h1>"
            + body.replace("{WHO}", who).replace("{CONTACT}", contact).replace("{BASE}", escape(s.public_base_url))
            + "<p style='color:#5a6677;font-size:14px;margin-top:40px'>Last updated: 23 November 2026</p></body></html>")
    return HTMLResponse(html)


@router.get("/privacy")
def privacy():
    return _page("Privacy Policy", """
<p>OmniQMS is a customer-message tool operated by {WHO}. Businesses connect their Facebook Pages, Instagram
professional accounts and YouTube channels so that comments and direct messages sent to those accounts can be
answered from one inbox, with optional AI-drafted replies.</p>
<h2>What we collect</h2>
<ul>
<li><b>Business users:</b> name, email address and a hashed password for the OmniQMS dashboard.</li>
<li><b>Connected accounts:</b> the Page, Instagram account or YouTube channel ID and name, and the access tokens
the platform issues when the business approves the connection. Tokens are encrypted at rest.</li>
<li><b>Messages and comments</b> sent to a connected account: the text, the sender's platform-scoped ID and display
name, and timestamps, plus the replies sent from OmniQMS.</li>
<li><b>Knowledge base</b> content that the business adds to answer questions.</li>
</ul>
<h2>How we use it</h2>
<p>Only to show messages to the business that owns the connected account, to draft and send the replies that
business approves or configures, and to keep the service secure. We do not sell data, use it for advertising, or
share it with other businesses using OmniQMS. Message text may be sent to an AI model provider (such as OpenAI or
Anthropic) solely to generate a reply; those providers process it on our behalf.</p>
<h2>Retention and deletion</h2>
<p>Data is kept while the business keeps its account connected. Disconnecting an account deletes its access tokens
immediately. See <a href="{BASE}/data-deletion">Data deletion</a> for how to remove all data.</p>
<h2>Security</h2>
<p>Traffic is encrypted with HTTPS, access tokens are encrypted in the database, and each business can only see its
own workspace.</p>
<h2>Contact</h2><p>Questions or requests: {CONTACT}.</p>
""")


@router.get("/data-deletion")
def data_deletion():
    return _page("Data Deletion Instructions", """
<p>You can remove the data OmniQMS holds about you at any time.</p>
<h2>If you are a business using OmniQMS</h2>
<ol>
<li>Open OmniQMS → <b>Connections</b> and click <b>Disconnect</b> on each account. Its access tokens are deleted
immediately and no new messages are collected.</li>
<li>To delete your whole workspace (messages, knowledge base and users), email {CONTACT} from your account's email
address. We delete it within 30 days and confirm by email.</li>
<li>You can also remove the app from Facebook: Settings &amp; privacy → Settings → Business integrations (or Apps and
websites) → remove OmniQMS.</li>
</ol>
<h2>If you messaged or commented on a business that uses OmniQMS</h2>
<p>Ask that business to delete your conversation, or email {CONTACT} with the Page or channel name and the date of
your message, and we will remove it within 30 days.</p>
""")


@router.get("/terms")
def terms():
    return _page("Terms of Service", """
<p>OmniQMS is provided by {WHO} to businesses that want to manage social media comments and messages. By connecting
an account you confirm you are allowed to manage it, and you remain responsible for the replies sent from it,
including AI-drafted replies you enable. Follow the Meta Platform Terms, Instagram and YouTube policies, and the
messaging windows they set. We may suspend accounts used for spam or abuse. The service is provided as is during
testing. Contact: {CONTACT}.</p>
""")
