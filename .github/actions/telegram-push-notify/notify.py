#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


MAX_COMMITS = 100  # newest commits shown before the "…" row
PER_PAGE = 100  # GitHub compare API page size (max 100)
# Rich messages allow 32768 characters. The docs don't say whether markup counts, so
# the raw HTML is measured (conservative) and commit subjects are shortened until it fits.
TG_RICH_LIMIT = 32000
SUBJECT_LIMITS = (120, 80, 50, 30)
ZERO_SHA = '0' * 40


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f'::error::{msg}', flush=True)
    sys.exit(1)


def env(name: str, default: str = '') -> str:
    return os.environ.get(name, '').strip() or default


def request_json(
    url: str,
    *,
    label: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 4,
) -> dict:
    """GET (or POST when `payload` is given) a URL and decode JSON.

    `label` is used in errors instead of the URL so the bot token never ends up in logs.
    Retries on 429 (honouring Telegram's retry_after) and 5xx.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs['Content-Type'] = 'application/json'

    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors='replace')
            delay: float | None = None
            if e.code == 429:
                try:
                    delay = float(json.loads(text)['parameters']['retry_after'])
                except Exception:
                    delay = 5
            elif e.code >= 500:
                delay = 2**attempt
            if delay is None or attempt == retries - 1:
                raise RuntimeError(f'{label} failed: HTTP {e.code}: {text}') from None
            log(f'{label}: HTTP {e.code}, retrying in {delay}s')
            time.sleep(delay)
        except urllib.error.URLError as e:
            if attempt == retries - 1:
                raise RuntimeError(f'{label} failed: {e.reason}') from None
            time.sleep(2**attempt)
    raise AssertionError('unreachable')


# --------------------------------------------------------------------------------------
# Commits
# --------------------------------------------------------------------------------------
@dataclass
class Commit:
    sha: str
    url: str
    message: str
    author_name: str
    author_login: str | None  # GitHub account, None if the email isn't linked to one

    @classmethod
    def from_api(cls, c: dict) -> Commit:
        return cls(
            sha=c['sha'],
            url=c['html_url'],
            message=c['commit']['message'],
            author_name=c['commit']['author']['name'],
            author_login=(c.get('author') or {}).get('login'),
        )

    @classmethod
    def from_event(cls, c: dict) -> Commit:
        author = c.get('author') or {}
        return cls(
            sha=c['id'],
            url=c['url'],
            message=c['message'],
            author_name=author.get('name', '?'),
            author_login=author.get('username'),
        )


class GitHub:
    def __init__(self, api_url: str, repo: str, token: str) -> None:
        self.base = f'{api_url}/repos/{repo}'
        self.headers = {
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'telegram-push-notify',
        }
        if token:
            self.headers['Authorization'] = f'Bearer {token}'

    def get(self, path: str, **params: object) -> dict:
        url = f'{self.base}/{path}'
        if params:
            url += '?' + urllib.parse.urlencode(params)
        return request_json(url, label=f'GitHub GET {path}', headers=self.headers)

    def compare(
        self, base: str, head: str
    ) -> tuple[int, list[Commit], Commit | None]:
        """Commits in base...head.

        Returns (total, newest commits oldest→newest (max MAX_COMMITS), oldest commit if
        it isn't already in that list). The compare API returns commits oldest-first.
        """
        path = f'compare/{urllib.parse.quote(base, safe="")}...{urllib.parse.quote(head, safe="")}'
        pages: dict[int, list[dict]] = {}

        def page(n: int) -> list[dict]:
            if n not in pages:
                pages[n] = self.get(path, per_page=PER_PAGE, page=n)['commits']
            return pages[n]

        first = self.get(path, per_page=PER_PAGE, page=1)
        pages[1] = first['commits']
        total = int(first['total_commits'])

        if total <= MAX_COMMITS:
            return total, [Commit.from_api(c) for c in pages[1]], None

        start = total - MAX_COMMITS  # index of the oldest commit we show in full
        first_page = start // PER_PAGE + 1
        last_page = (total - 1) // PER_PAGE + 1
        window: list[dict] = []
        for n in range(first_page, last_page + 1):
            window.extend(page(n))
        newest = window[start - (first_page - 1) * PER_PAGE :]

        oldest = Commit.from_api(pages[1][0])
        return total, [Commit.from_api(c) for c in newest], oldest


# --------------------------------------------------------------------------------------
# Formatting (Telegram HTML)
# --------------------------------------------------------------------------------------
def esc(text: str) -> str:
    return html.escape(text, quote=True)


def link(url: str, text: str) -> str:
    return f'<a href="{esc(url)}">{esc(text)}</a>'


def subject(message: str, limit: int) -> str:
    lines = message.strip().splitlines()
    first = lines[0].strip() if lines else ''
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + '…'
    return first or '(без сообщения)'


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """plural_ru(5, 'коммит', 'коммита', 'коммитов') -> 'коммитов'."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def commit_row(c: Commit, server_url: str, subject_limit: int) -> str:
    if c.author_login and c.author_login.endswith('[bot]'):
        # Bot accounts (dependabot[bot], github-actions[bot]) live under /apps/<name>.
        author = link(f'{server_url}/apps/{c.author_login.removesuffix("[bot]")}', c.author_login)
    elif c.author_login:
        author = link(f'{server_url}/{c.author_login}', c.author_login)
    else:
        author = esc(c.author_name)
    return (
        f'<tr><td>{link(c.url, c.sha[:7])}</td>'
        f'<td>{author}</td>'
        f'<td>{esc(subject(c.message, subject_limit))}</td></tr>'
    )


def commits_table(
    newest: list[Commit], oldest: Commit | None, total: int, server_url: str, subject_limit: int
) -> str:
    """Table of commits, newest → oldest. `newest` is given oldest → newest."""
    rows = ['<tr><th>Коммит</th><th>Автор</th><th>Сообщение</th></tr>']
    rows += [commit_row(c, server_url, subject_limit) for c in reversed(newest)]
    if oldest is not None:
        skipped = total - len(newest) - 1
        if skipped > 0:
            word = plural_ru(skipped, 'коммит', 'коммита', 'коммитов')
            rows.append(
                f'<tr><td colspan="3" align="center">… <i>ещё {skipped} {word}</i></td></tr>'
            )
        rows.append(commit_row(oldest, server_url, subject_limit))
    return '<table bordered striped compact>\n' + '\n'.join(rows) + '\n</table>'


def render_message(
    *,
    server_url: str,
    repo: str,
    ref: str,
    after: str,
    total: int,
    newest: list[Commit],
    oldest: Commit | None,
    compare_url: str | None,
    created: bool,
    forced: bool,
) -> str:
    """Full rich-message HTML: header + commits table (newest → oldest)."""
    ref_name = ref.removeprefix('refs/heads/').removeprefix('refs/tags/')
    repo_link = link(f'{server_url}/{repo}', repo)
    branch_link = link(f'{server_url}/{repo}/tree/{ref_name}', ref_name)
    tags = []
    if created:
        tags.append('новая ветка' if ref.startswith('refs/heads/') else 'новый тег')
    if forced:
        tags.append('force-push')
    tag_str = f' <i>({", ".join(tags)})</i>' if tags else ''
    head_link = link(f'{server_url}/{repo}/commit/{after}', after[:7])
    commits_line = f'<b>Коммитов:</b> {total}'
    if compare_url:
        commits_line += f' ({link(compare_url, "сравнение")})'

    header = '\n'.join(
        [
            f'<p>🔔 <b>Новый пуш</b> в {repo_link}</p>',
            f'<p><b>Ветка:</b> {branch_link}{tag_str}</p>',
            f'<p><b>Текущий HEAD:</b> {head_link}</p>',
            f'<p>{commits_line}</p>',
        ]
    )

    if not newest:
        return header + '\n<p><i>Новых коммитов нет.</i></p>'

    rich_html = ''
    for limit in SUBJECT_LIMITS:
        rich_html = header + '\n' + commits_table(newest, oldest, total, server_url, limit)
        if len(rich_html) <= TG_RICH_LIMIT:
            break
        log(f'Message is {len(rich_html)} chars with {limit}-char subjects, shortening')
    return rich_html


# --------------------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------------------
def send_telegram(token: str, chat_id: str, thread_id: str, rich_html: str) -> None:
    payload: dict = {
        'chat_id': chat_id,
        # skip_entity_detection: don't turn "#123" / "@user" in commit messages into
        # hashtags / mentions.
        'rich_message': {'html': rich_html, 'skip_entity_detection': True},
    }
    if thread_id:
        payload['message_thread_id'] = int(thread_id)
    result = request_json(
        f'https://api.telegram.org/bot{token}/sendRichMessage',
        label='Telegram sendRichMessage',
        payload=payload,
    )
    if not result.get('ok'):
        raise RuntimeError(f'Telegram sendRichMessage failed: {result}')


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> None:
    dry_run = env('DRY_RUN') == '1'
    token = env('TELEGRAM_BOT_TOKEN')
    chat_id = env('TELEGRAM_CHAT_ID')
    thread_id = env('TELEGRAM_THREAD_ID')
    if not dry_run:
        if not token:
            fail('TELEGRAM_BOT_TOKEN is empty (organization secret missing or not passed?)')
        if not chat_id:
            fail('TELEGRAM_CHAT_ID is empty (organization variable missing?)')
        if thread_id and not thread_id.lstrip('-').isdigit():
            fail(f'TELEGRAM_THREAD_ID must be a number, got {thread_id!r}')

    event_path = env('GITHUB_EVENT_PATH')
    if not event_path:
        fail('GITHUB_EVENT_PATH is not set; this script must run in GitHub Actions')
    with open(event_path, encoding='utf-8') as f:
        event = json.load(f)

    if 'after' not in event:
        fail('This action only works on `push` events')
    if event.get('deleted'):
        log('Ref was deleted, nothing to notify about.')
        return

    server_url = env('GITHUB_SERVER_URL', 'https://github.com')
    repo = env('GITHUB_REPOSITORY') or event['repository']['full_name']
    gh = GitHub(env('GITHUB_API_URL', 'https://api.github.com'), repo, env('GITHUB_TOKEN'))

    ref: str = event['ref']
    before: str = event['before']
    after: str = event['after']
    default_branch: str = event['repository'].get('default_branch', '')
    created = event.get('created') or before == ZERO_SHA

    # A new branch has no "before", so compare it to the default branch instead.
    base: str | None
    if not created:
        base = before
    elif default_branch and ref != f'refs/heads/{default_branch}':
        base = default_branch
    else:
        base = None

    total: int
    newest: list[Commit]
    oldest: Commit | None = None
    compare_url: str | None = None
    try:
        if base is None:
            raise LookupError('nothing to compare against')
        total, newest, oldest = gh.compare(base, after)
        compare_url = f'{server_url}/{repo}/compare/{base}...{after}'
    except Exception as e:
        # E.g. `before` was garbage collected after a force push, or first push to repo.
        # The event payload has the commits too, but GitHub caps how many it includes.
        log(f'::warning::Compare API unavailable ({e}); using commits from the event payload')
        payload_commits = [Commit.from_event(c) for c in event.get('commits') or []]
        total = len(payload_commits)
        newest = payload_commits[-MAX_COMMITS:]
        if total > MAX_COMMITS:
            oldest = payload_commits[0]
        compare_url = event.get('compare')

    rich_html = render_message(
        server_url=server_url,
        repo=repo,
        ref=ref,
        after=after,
        total=total,
        newest=newest,
        oldest=oldest,
        compare_url=compare_url,
        created=bool(created),
        forced=bool(event.get('forced')),
    )

    if dry_run:
        log(f'----- rich message ({len(rich_html)} chars) -----')
        log(rich_html)
        return

    send_telegram(token, chat_id, thread_id, rich_html)
    log(f'Sent notification about {total} commit(s).')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as e:
        fail(str(e))
