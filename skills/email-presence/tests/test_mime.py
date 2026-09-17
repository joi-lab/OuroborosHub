from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default

from lib.mime import build_message, extract_body, parse_message


def test_parse_plain_and_thread_headers():
    msg=EmailMessage(); msg["Message-ID"]="<m1@example.com>"; msg["From"]="Alice <alice@example.com>"; msg["To"]="bot@example.com"; msg["Subject"]="Hello"; msg["References"]="<root@example.com>"; msg.set_content("Привет")
    item=parse_message(msg.as_bytes(),folder="INBOX",uid=7)
    assert item["message_id"]=="<m1@example.com>"; assert item["sender"]=="alice@example.com"; assert item["body"]=="Привет"; assert item["references"]==["<root@example.com>"]

def test_html_fallback_and_reply_headers():
    out=build_message(sender="bot@example.com",recipients=["a@example.com"],subject="Re: Hi",body="Reply",in_reply_to="<m1@example.com>",references=["<root@example.com>"])
    assert out["In-Reply-To"]=="<m1@example.com>"; assert "<root@example.com>" in out["References"] and "<m1@example.com>" in out["References"]
    html=EmailMessage(); html["From"]="a@example.com"; html["To"]="bot@example.com"; html["Subject"]="x"; html.set_content("<p>Hello<br>world</p>",subtype="html")
    assert extract_body(BytesParser(policy=default).parsebytes(html.as_bytes()))=="Hello\nworld"
