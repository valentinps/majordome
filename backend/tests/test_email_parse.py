from email.message import EmailMessage

from majordome.ingest.email_parse import clean_subject, html_to_text, parse_email
from majordome.ingest.normalize import AccountRef

ACC = AccountRef("email", "me@example.com", "Gmail")


def build(subject="Votre machine à laver", body="Le cycle est terminé.", html=None, **headers):
    m = EmailMessage()
    m["From"] = headers.pop("from_", "Bosch Home <noreply@bosch.example>")
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = "Mon, 06 Oct 2026 14:32:00 +0200"
    m["Message-ID"] = headers.pop("msgid", "<abc@bosch.example>")
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def parse(m, **kw):
    return parse_email(m.as_bytes(), ACC, folder="INBOX", uid=42, uidvalidity=7, **kw)


def test_plain_email():
    msg = parse(build())
    assert msg.subject == "Votre machine à laver"
    assert msg.body_text == "Le cycle est terminé."
    assert msg.sender.external_id == "noreply@bosch.example"
    assert msg.sender.display_name == "Bosch Home"
    assert msg.external_id == "<abc@bosch.example>"
    assert msg.sent_at.isoformat() == "2026-10-06T14:32:00+02:00"
    assert msg.direction == "in" and msg.ingest_channel == "imap"
    assert msg.raw["headers"]["Message-ID"] == "<abc@bosch.example>"


def test_prefers_plain_but_falls_back_to_html():
    m = EmailMessage()
    m["From"] = "a@b.c"
    m["Subject"] = "html only"
    m.set_content("<html><head><style>x{}</style></head><body><p>Hello</p><p>World</p>"
                  "<script>evil()</script></body></html>", subtype="html")
    msg = parse(m)
    assert msg.body_text == "Hello\nWorld" and msg.raw["body_format"] == "html"


def test_threading():
    assert parse(build()).conversation.external_id == "<abc@bosch.example>"
    reply = build(subject="RE: Tr: Votre machine à laver", msgid="<r2@x>",
                  In_Reply_To="<r1@x>", References="<root@x> <r1@x>")
    parsed = parse(reply)
    assert parsed.conversation.external_id == "<root@x>"
    assert parsed.conversation.title == "Votre machine à laver"
    assert parse(build(), gmail_thread_id=123).conversation.external_id == "gm:123"


def test_attachments_and_direction_out():
    m = build(from_="Me <ME@example.com>")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                     filename="facture.pdf")
    msg = parse(m)
    assert msg.direction == "out"
    assert [(a.filename, a.mime_type, a.size) for a in msg.attachments] == [
        ("facture.pdf", "application/pdf", 13)]


def test_missing_message_id_uses_uid():
    m = build()
    del m["Message-ID"]
    assert parse(m).external_id == "uid:INBOX:7:42"


def test_helpers():
    assert clean_subject("Re: RE: Fwd: hello") == "hello"
    assert html_to_text("<p>a</p>\n\n\n\n<p>b</p>") == "a\n\nb"
