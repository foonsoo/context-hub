import pytest

from context_hub.secrets import REDACTED, redact

SECRETS = [
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-123456789012-abcdefghijkl",
    "AIzaSyA-abcdefghijklmnopqrstuvwxyz012345",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_known_formats_are_redacted(secret):
    out = redact(f"앞 {secret} 뒤")
    assert secret not in out and REDACTED in out
    assert out.startswith("앞 ") and out.endswith(" 뒤")


@pytest.mark.parametrize("text,leak", [
    ("DATABASE_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ("export OPENAI_API_KEY='abcdef123456'", "abcdef123456"),
    ('client_secret: "s3cr3tvalue!"', "s3cr3tvalue!"),
    ("Authorization: Bearer abc.def.ghijklmnop", "abc.def.ghijklmnop"),
    ("postgres://admin:supersecret@db.internal/app", "supersecret"),
])
def test_assignments_and_urls(text, leak):
    out = redact(text)
    assert leak not in out and REDACTED in out


@pytest.mark.parametrize("text", [
    "token budget 2000으로 줄였다",
    "password policy는 12자 이상",
    "API_KEY=${API_KEY} 로 주입한다",
    "SECRET_NAME=<your secret>",
    "sk-short",
    "결정: 웹훅은 5회 재시도",
])
def test_benign_text_is_untouched(text):
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "yield_time_ms:1000,max_output_tokens:2000 text(r.output)",
    "MAX_TOKENS=4096",
    'secret=$(openssl rand -hex 32)',
    "TOKEN_ENABLED=true",
    "API_KEY=$API_KEY",
])
def test_counts_and_references_are_not_secrets(text):
    assert redact(text) == text


@pytest.mark.parametrize("text,leak", [
    ("sh -lc 'PGPASSWORD=Xk82!mq psql -U app'", "Xk82!mq"),
    ('-H "X-Atlassian-Token: ATATT3xFfGF0abc"', "ATATT3xFfGF0abc"),
    ("kubectl create secret --from-literal=OPEN_API_KEY=abc123def456", "abc123def456"),
])
def test_real_world_leaks_from_transcripts(text, leak):
    assert leak not in redact(text)
