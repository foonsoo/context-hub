"""`ctx` command line."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, adapters, curator, hooks, knowledge, legacy, ops, review, setup
from .hub import Hub, Project, default_root, slugify
from .identity import (
    canonical_path, label_owner, normalize_label, project_for_cwd, repo_info, resolve,
    suggest as identity_suggest,
)
from .index import Index
from .mcp import serve
from .recall import recall


def _hub(args) -> Hub:
    return Hub(args.home) if getattr(args, "home", None) else Hub()


def _require(hub: Hub) -> Hub:
    if not hub.exists():
        sys.exit(f"Context Hub가 아직 없습니다 ({hub.root}). 먼저 `ctx init`을 실행하세요.")
    return hub


def _lines(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(";") if part.strip())
    return out


# commands --------------------------------------------------------------------

def cmd_init(args) -> int:
    hub = _hub(args)
    created = setup.init_hub(hub)
    print(("Context Hub를 만들었습니다: " if created else "Context Hub가 이미 있습니다: ") + str(hub.root))
    clients = args.clients.split(",") if args.clients else None
    report = setup.plan_clients(clients, hooks=not args.no_hooks)
    if args.register:
        setup.apply_clients(report)
        print(report.render(applied=True))
        print("\n클라이언트를 다시 시작하면 적용됩니다.")
    else:
        print(report.render(applied=False))
    if args.import_legacy:
        return _import(hub, Path(args.import_legacy), apply=args.register, include_proposed=False)
    legacy_db = Path.home() / ".local/share/context-memory/memory.db"
    if legacy_db.exists() and not hub.read_state("legacy").get("imported"):
        print(f"\n기존 context-memory DB가 있습니다: {legacy_db}\n  가져오기: ctx import-legacy --apply")
    return 0


def cmd_uninstall(args) -> int:
    report = setup.unregister(args.clients.split(",") if args.clients else None)
    print(report.render(applied=True))
    print("허브 폴더와 지식은 삭제하지 않았습니다.")
    return 0


def cmd_recall(args) -> int:
    hub = _require(_hub(args))
    result = recall(hub, " ".join(args.query), cwd=os.getcwd(), project=args.project, budget=args.budget)
    print(result.text)
    return 0


def cmd_note(args) -> int:
    hub = _require(_hub(args))
    text = " ".join(args.text) if args.text else sys.stdin.read()
    result = ops.note(hub, text, cwd=os.getcwd(), kind=args.kind, project=args.project, client="cli",
                      confirmed=not args.proposed)
    print(result.message)
    return 0 if result.ok else 1


def cmd_handoff(args) -> int:
    hub = _require(_hub(args))
    result = ops.handoff(hub, cwd=os.getcwd(), done=_lines(args.done), next_steps=_lines(args.next),
                         blockers=_lines(args.blocker) if args.blocker is not None or args.clear_blockers else None,
                         goal=args.goal, in_progress=_lines(args.doing), project=args.project, client="cli",
                         add_next=args.add_next)
    print(result.message)
    return 0 if result.ok else 1


def _unknown_project(hub: Hub, name: str) -> int:
    hints = identity_suggest(name, hub.projects())
    hint = f" 혹시 {', '.join(hints)}을(를) 찾으셨나요?" if hints else " `ctx projects`로 목록을 확인하세요."
    print(f"'{name}' 프로젝트를 찾지 못했습니다.{hint}")
    return 1


def cmd_now(args) -> int:
    hub = _require(_hub(args))
    projects = hub.projects()
    if args.all:
        slugs = sorted(projects)
    elif args.project:
        res = resolve(hub, project=args.project)
        if res.ambiguous:
            print("이름이 여러 프로젝트에 해당합니다: " + ", ".join(res.ambiguous[0]))
            return 1
        if not res.slugs:
            return _unknown_project(hub, args.project)
        slugs = res.slugs
    else:
        res = resolve(hub, cwd=os.getcwd())
        if not res.slugs:
            if repo_info(os.getcwd()):
                print("아직 등록되지 않은 git 저장소입니다. 첫 note/handoff 때 자동으로 등록됩니다.")
            else:
                print("현재 폴더는 프로젝트가 아닙니다. `ctx now <프로젝트>` 또는 `ctx now --all`을 쓰세요.")
            return 1
        slugs = res.slugs
    for slug in slugs:
        doc = knowledge.load_now(hub, slug, projects[slug].display if slug in projects else slug)
        if doc.is_empty():
            print(f"# NOW · {doc.title}\n(아직 비어 있습니다. `ctx handoff --next ...`나 에이전트의 handoff로 채워집니다.)")
        else:
            print(doc.text().strip())
        print()
    return 0


def cmd_projects(args) -> int:
    hub = _require(_hub(args))
    projects = hub.projects()
    action = args.action
    if action != "list" and action != "add" and args.slug not in projects:
        if not args.slug:
            sys.exit(f"사용법: ctx projects {action} <slug> ...")
        return _unknown_project(hub, args.slug)
    if action == "alias":
        if not args.values:
            sys.exit("사용법: ctx projects alias <slug> <별칭...>")
        aliases = list(projects[args.slug].aliases)
        for alias in args.values:
            owner = label_owner(alias, projects, exclude=args.slug)
            if owner:
                print(f"'{alias}'는 이미 {owner} 프로젝트의 이름/별칭이라 추가하지 않았습니다.")
            elif alias not in aliases:
                aliases.append(alias)
        hub.update_project(args.slug, aliases=aliases)
        hub.commit(f"alias: {args.slug}")
        print(f"{args.slug} 별칭: {', '.join(aliases) or '(없음)'}")
        return 0
    if action == "unalias":
        drop = {normalize_label(v) for v in args.values}
        aliases = [a for a in projects[args.slug].aliases if normalize_label(a) not in drop]
        hub.update_project(args.slug, aliases=aliases)
        hub.commit(f"unalias: {args.slug}")
        print(f"{args.slug} 별칭: {', '.join(aliases) or '(없음)'}")
        return 0
    if action == "rename":
        if not args.values:
            sys.exit("사용법: ctx projects rename <slug> <표시 이름>")
        hub.update_project(args.slug, name=" ".join(args.values))
        hub.commit(f"rename: {args.slug}")
        print(f"{args.slug} 표시 이름: {' '.join(args.values)}")
        return 0
    if action == "capture":
        if not args.values or args.values[0] not in ("on", "off"):
            sys.exit("사용법: ctx projects capture <slug> on|off")
        hub.update_project(args.slug, capture=args.values[0] == "on")
        hub.commit(f"capture {args.values[0]}: {args.slug}")
        print(f"{args.slug} 대화 기록 수집: {args.values[0]}")
        return 0
    if action == "remove":
        del projects[args.slug]
        hub.save_projects(projects)
        hub.commit(f"remove project: {args.slug}")
        print(f"등록을 해제했습니다: {args.slug}. 지식 파일(knowledge/projects/{args.slug})은 그대로 남아 있습니다.")
        return 0
    if action == "add":
        path = canonical_path(args.slug or os.getcwd())
        if not os.path.isdir(path):
            sys.exit(f"폴더가 없습니다: {path}")
        info = repo_info(path)
        name = " ".join(args.values) if args.values else Path(path).name
        folder = Path(info.common_root if info else path).name
        owner, _ = project_for_cwd(hub, path, projects)
        if owner:
            print(f"이미 {owner} 프로젝트에 속한 폴더입니다.")
            return 1
        project = hub.add_project(Project(
            slug=slugify(folder), name=name,
            remotes=[info.remote] if info and info.remote else [],
            paths=[info.common_root if info else path],
        ))
        hub.commit(f"project: {project.slug}")
        print(f"등록: {project.slug} ({project.display})")
        return 0
    here, how = project_for_cwd(hub, os.getcwd(), projects)
    if not projects:
        print("등록된 프로젝트가 없습니다. git 저장소에서 첫 note/handoff를 하면 자동으로 등록됩니다.")
        return 0
    print(f"  {'SLUG':<24} {'표시 이름':<20} 별칭")
    for project in projects.values():
        mark = "*" if project.slug == here else " "
        capture = "  (수집 꺼짐)" if not project.capture else ""
        print(f"{mark} {project.slug:<24} {project.display:<20} {', '.join(project.aliases)}{capture}")
    print("\n* = 현재 폴더의 프로젝트")
    return 0


def _review_targets(hub: Hub, ids: list[str]) -> list:
    found = []
    for value in ids:
        item = review.get(hub, value)
        if item:
            found.append(item)
            continue
        by_page = review.for_page(hub, value)
        if by_page:
            found.extend(by_page)
        else:
            print(f"검토 항목을 찾지 못했습니다: {value}")
    return found


def cmd_review(args) -> int:
    hub = _require(_hub(args))
    items = review.pending(hub, project=args.project)
    if args.action == "list":
        if not items:
            print("검토할 항목이 없습니다.")
        for item in items:
            print(review.describe(hub, item))
            print()
        if items:
            print("승인: ctx review approve <rev-… 또는 지식 id>  ·  기각: ctx review reject <id>  ·  모두: --all")
        return 0
    if args.action in ("approve", "reject"):
        targets = items if args.all else _review_targets(hub, args.ids)
        if not targets:
            print("대상 항목이 없습니다. `ctx review list`로 확인하세요.")
            return 1
        for item in targets:
            print(review.approve(hub, item) if args.action == "approve" else review.reject(hub, item))
        hub.commit(f"review: {args.action} {len(targets)}")
        return 0
    if not items:
        print("검토할 항목이 없습니다.")
        return 0
    print(f"검토할 항목 {len(items)}개. y=승인 n=기각 e=편집 후 승인 s=건너뛰기 q=끝내기\n")
    counts = {"y": 0, "n": 0, "e": 0, "s": 0}
    for item in items:
        print(review.describe(hub, item))
        while True:
            try:
                answer = input("  [y/n/e/s/q] > ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in ("y", "n", "e", "s", "q"):
                break
            print("  y, n, e, s, q 중 하나를 입력하세요.")
        if answer == "q":
            break
        if answer == "y":
            print("  " + review.approve(hub, item))
        elif answer == "n":
            print("  " + review.reject(hub, item))
        elif answer == "e":
            print("  " + review.edit_then_approve(hub, item))
        counts[answer] += 1
        print()
    left = len(review.pending(hub, project=args.project))
    print(f"승인 {counts['y'] + counts['e']}, 기각 {counts['n']}, 건너뜀 {counts['s']} · 남은 항목 {left}개")
    if counts["y"] + counts["e"] + counts["n"]:
        hub.commit(f"review: {counts['y'] + counts['e'] + counts['n']} items")
    return 0


def cmd_curate(args) -> int:
    hub = _require(_hub(args))
    quiet = 0 if args.now else curator.QUIET_SECONDS
    if not args.no_ingest:
        adapters.ingest(hub, quiet_seconds=0)
    extractor = curator.default_extractor(hub)
    report = curator.curate(hub, extractor, quiet_seconds=quiet)
    if not args.quiet:
        print(report.summary())
        for line in report.skipped + report.errors:
            print("  " + line)
    return 0


def cmd_ingest(args) -> int:
    hub = _require(_hub(args))
    names = args.clients.split(",") if args.clients else None
    result = adapters.ingest(hub, names=names, paths=[Path(p) for p in args.paths] if args.paths else None)
    hub.commit("ingest transcripts")
    for name, count in result.items():
        print(f"{name}: 새 이벤트 {count}개")
    return 0


def cmd_status(args) -> int:
    hub = _require(_hub(args))
    projects = hub.projects()
    with Index(hub) as index:
        rows = index.pages()
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    usage = hub.read_state("curator").get("usage", {})
    found = curator.default_extractor(hub)
    if found:
        extractor = "Claude Code CLI" if isinstance(found, curator.ClaudeCliExtractor) else "Anthropic API"
    else:
        extractor = ("꺼짐 (API: pip install 'context-hub[llm]' + ANTHROPIC_API_KEY, "
                     "또는 Claude Code 구독으로: CTX_CURATOR=claude)")
    here, how = project_for_cwd(hub, os.getcwd(), projects)
    print(f"허브: {hub.root}")
    print(f"현재 폴더 프로젝트: {here or '없음'}" + (f" ({how})" if here else ""))
    print(f"프로젝트: {len(projects)}개")
    print("지식: " + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "없음"))
    print(f"검토 대기: {len(review.pending(hub))}개")
    print(f"정리 대기 세션: {len(curator.pending(hub, quiet_seconds=0))}개")
    print(f"LLM 큐레이터: {extractor} · 오늘 호출 {usage.get(knowledge.today(), 0)}회")
    return 0


def cmd_reindex(args) -> int:
    hub = _require(_hub(args))
    with Index(hub) as index:
        count = index.rebuild()
    print(f"인덱스를 다시 만들었습니다: 페이지 {count}개")
    return 0


def cmd_forget(args) -> int:
    hub = _require(_hub(args))
    page = knowledge.find(hub, args.id)
    if not page:
        print(f"지식을 찾지 못했습니다: {args.id}")
        return 1
    if page.status == "archived":
        print(f"이미 보관된 지식입니다: {page.title}")
        return 0
    page.meta["status"] = "archived"
    knowledge.save(hub, page, by_curator=not page.human_edited())
    review.forget_in_now(hub, page)
    closed = review.close_for_page(hub, page.id)
    hub.commit(f"forget: {page.title}")
    if closed:
        print(f"관련 검토 항목 {closed}개도 닫았습니다.")
    print(f"보관 처리했습니다: {page.title} (증거는 남아 있음)")
    return 0


def cmd_show(args) -> int:
    hub = _require(_hub(args))
    page = knowledge.find(hub, args.id)
    if not page:
        print(f"지식을 찾지 못했습니다: {args.id}")
        return 1
    print(page.path)
    print(page.path.read_text(encoding="utf-8"))
    if args.sources:
        from .inbox import resolve_ref
        for ref in page.sources:
            entry = resolve_ref(hub, ref)
            print(f"--- {ref}\n{(entry or {}).get('text', '(찾을 수 없음)')}")
    return 0


def cmd_sync(args) -> int:
    hub = _require(_hub(args))
    if not hub.is_git():
        sys.exit("허브가 git 저장소가 아닙니다.")
    if args.action == "add":
        if not args.url:
            sys.exit("사용법: ctx sync add <git 원격 주소>")
        print("주의: 허브에는 작업 대화 요약이 들어갑니다. 반드시 비공개 저장소를 사용하세요.")
        hub.git("remote", "remove", "origin")
        result = hub.git("remote", "add", "origin", args.url)
        print("원격을 등록했습니다." if result.returncode == 0 else result.stderr)
        return result.returncode
    hub.commit("sync")
    if not hub.git("remote").stdout.strip():
        sys.exit("원격이 없습니다. `ctx sync add <주소>`로 등록하세요.")
    branch = hub.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    pull = hub.git("pull", "--rebase", "origin", branch)
    if pull.returncode != 0 and "couldn't find remote ref" not in pull.stderr:
        print(pull.stderr.strip())
        return 1
    push = hub.git("push", "-u", "origin", branch)
    print("동기화했습니다." if push.returncode == 0 else push.stderr.strip())
    return push.returncode


def cmd_import(args) -> int:
    hub = _hub(args)
    hub.ensure()
    return _import(hub, Path(args.db).expanduser(), apply=args.apply, include_proposed=args.include_proposed,
                   merge_by_name=not args.no_merge_by_name)


def _import(hub: Hub, db: Path, apply: bool, include_proposed: bool, merge_by_name: bool = True) -> int:
    if not db.exists():
        print(f"DB가 없습니다: {db}")
        return 1
    statuses = ("active", "proposed") if include_proposed else ("active",)
    the_plan = legacy.plan(hub, db, statuses, merge_by_name=merge_by_name)
    print(the_plan.describe())
    if not apply:
        print("\n미리보기입니다. 적용하려면 --apply를 붙이세요." + ("" if include_proposed else " (proposed까지: --include-proposed)"))
        return 0
    count = legacy.apply(hub, db, the_plan)
    print(f"\n{count}건을 가져왔습니다.")
    return 0


CONFIG_KEYS = {
    "curator": ("curator", "mode", ("off", "claude", "api")),
    "daily-calls": ("curator", "daily_calls", None),
    "model": ("curator", "model", None),
}


def cmd_config(args) -> int:
    import shutil

    hub = _require(_hub(args))
    if not args.key:
        print(f"설정 파일: {hub.config_path}")
        mode = hub.setting("curator", "mode", "CTX_CURATOR", "api")
        print(f"curator     = {mode}")
        print(f"daily-calls = {hub.setting('curator', 'daily_calls', 'CTX_CURATOR_DAILY_CALLS', curator.DEFAULT_DAILY_CALLS)}")
        print(f"model       = {hub.setting('curator', 'model', 'CTX_CURATOR_MODEL') or '(기본)'}")
        return 0
    section, key, allowed = CONFIG_KEYS[args.key]
    if args.value is None:
        print(hub.setting(section, key))
        return 0
    value: object = args.value
    if allowed and value not in allowed:
        sys.exit(f"{args.key}는 {', '.join(allowed)} 중 하나여야 합니다.")
    if args.key == "daily-calls":
        value = int(value)
    if args.key == "model" and value in ("", "default"):
        value = None
    hub.set_setting(section, key, value)
    if args.key == "curator" and value == "claude":
        found = shutil.which("claude")
        if not found:
            sys.exit("claude 명령을 찾지 못했습니다. Claude Code를 설치하고 로그인한 뒤 다시 실행하세요.")
        hub.set_setting("curator", "command", found)
    hub.commit(f"config: {args.key}")
    extractor = curator.default_extractor(hub)
    print(f"{args.key} = {value}" + ("" if args.key != "curator" or value == "off" else
          f" ({'사용 가능' if extractor else '사용 불가: 설정을 확인하세요'})"))
    return 0


def cmd_hook(args) -> int:
    return hooks.run(args.action, _hub(args))


def cmd_serve(args) -> int:
    serve(_hub(args))
    return 0


def cmd_daemon(args) -> int:
    hub = _require(_hub(args))
    if args.action == "run":
        print(f"{args.interval}초마다 수집·정리합니다. Ctrl-C로 멈춥니다.")
        while True:
            try:
                adapters.ingest(hub)
                curator.curate(hub, curator.default_extractor(hub))
            except Exception as exc:
                hub.log(f"daemon: {exc!r}")
            time.sleep(args.interval)
    if args.action == "install":
        if sys.platform != "darwin":
            sys.exit("자동 실행 등록은 현재 macOS(launchd)만 지원합니다. `ctx daemon run`을 서비스로 등록하세요.")
        plist = Path.home() / "Library/LaunchAgents/dev.context-hub.curator.plist"
        command = setup.ctx_command()
        args_xml = "".join(f"<string>{c}</string>" for c in [*command, "curate", "--quiet"])
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
            "<key>Label</key><string>dev.context-hub.curator</string>"
            f"<key>ProgramArguments</key><array>{args_xml}</array>"
            f"<key>StartInterval</key><integer>{args.interval}</integer>"
            f"<key>EnvironmentVariables</key><dict><key>CTX_HOME</key><string>{hub.root}</string></dict>"
            "<key>RunAtLoad</key><true/></dict></plist>\n"
        )
        os.system(f"launchctl unload {plist} 2>/dev/null; launchctl load {plist}")
        print(f"등록했습니다: {plist} ({args.interval}초 간격)")
        return 0
    if args.action == "uninstall":
        plist = Path.home() / "Library/LaunchAgents/dev.context-hub.curator.plist"
        os.system(f"launchctl unload {plist} 2>/dev/null")
        plist.unlink(missing_ok=True)
        print("해제했습니다.")
        return 0
    return 0


# parser ------------------------------------------------------------------------

EXAMPLES = """\
자주 쓰는 명령:
  ctx init --register                 처음 한 번: 허브 생성 + 클라이언트 등록
  ctx recall "B 프로젝트 진행 상태"     맥락 조회 (다른 프로젝트도 이름으로)
  ctx note "결정: 주문 API는 v2만"      지식 기록 (직접 쓴 기록은 바로 active)
  ctx handoff --done "로그인" --next "결제;배포"
  ctx now [프로젝트|--all]             현재 상태 보기
  ctx review                          에이전트·큐레이터 제안 검토
자세한 도움말: ctx <명령> --help
"""


def _parser(**kwargs) -> argparse.ArgumentParser:
    try:
        return argparse.ArgumentParser(suggest_on_error=True, **kwargs)
    except TypeError:  # Python < 3.14
        return argparse.ArgumentParser(**kwargs)


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = _parser(prog="ctx", description="AI 클라이언트 간 맥락 공유와 지식화", epilog=EXAMPLES,
                     formatter_class=raw)
    parser.add_argument("--version", action="version", version=f"ctx {__version__}")
    parser.add_argument("--home", help=f"허브 위치 (기본: $CTX_HOME 또는 {default_root()})")
    sub = parser.add_subparsers(dest="command", metavar="<명령>")

    def command(name: str, help: str, description: str | None = None, epilog: str | None = None):
        kwargs = {"help": help, "description": description or help, "formatter_class": raw}
        if epilog:
            kwargs["epilog"] = epilog
        try:
            return sub.add_parser(name, suggest_on_error=True, **kwargs)
        except TypeError:
            return sub.add_parser(name, **kwargs)

    p = command("init", "허브를 만들고 클라이언트 등록 계획을 보여 줍니다",
                epilog="--register 없이 실행하면 아무 설정도 바꾸지 않고 계획만 보여 줍니다.")
    p.add_argument("--register", action="store_true", help="MCP 서버·규칙·훅을 실제로 등록 (바꾸기 전 .context-hub.bak 백업)")
    p.add_argument("--clients", help="등록할 클라이언트: claude-code,codex,cursor 중 일부 (기본: 설치된 것 전부)")
    p.add_argument("--no-hooks", action="store_true", help="Claude Code 훅은 등록하지 않음")
    p.add_argument("--import-legacy", metavar="DB", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_init)

    p = command("uninstall", "클라이언트 등록·규칙·훅을 제거합니다 (허브의 지식은 그대로)")
    p.add_argument("--clients", help="claude-code,codex,cursor 중 일부")
    p.set_defaults(func=cmd_uninstall)

    p = command("recall", "맥락 조회: NOW.md + 관련 지식",
                epilog='예: ctx recall "왜 Postgres로 바꿨지"\n    ctx recall -p shop "할 일"\n    ctx recall -p "*" "전체 할 일"')
    p.add_argument("query", nargs="+", help="질문 (프로젝트 이름이 들어 있으면 그 프로젝트를 봅니다)")
    p.add_argument("-p", "--project", help="프로젝트 이름·별칭, 전체는 '*'")
    p.add_argument("-b", "--budget", type=int, default=2000, help="응답 크기 상한(토큰 근사, 기본 2000)")
    p.set_defaults(func=cmd_recall)

    p = command("note", "지식 한 건 기록",
                description="지식 한 건을 기록합니다. 직접 입력한 기록은 바로 active가 되고,\n"
                            "에이전트가 MCP로 남긴 기록은 proposed로 검토를 기다립니다.",
                epilog='예: ctx note "결정: 결제는 Toss로 간다"\n    ctx note -k constraint "프로덕션 DB 직접 수정 금지"\n'
                       '    echo "긴 메모" | ctx note -p shop')
    p.add_argument("text", nargs="*", help="내용 (생략하면 표준 입력에서 읽음)")
    p.add_argument("-k", "--kind", choices=sorted(knowledge.TYPES), help="종류 (생략하면 내용으로 추정)")
    p.add_argument("-p", "--project", help="프로젝트 이름·별칭 ('global' = 프로젝트 무관)")
    p.add_argument("--proposed", action="store_true", help="바로 확정하지 않고 검토 대기로 남김")
    p.set_defaults(func=cmd_note)

    p = command("handoff", "NOW.md 갱신: 한 일, 다음 할 일, 막힌 일",
                description="NOW.md를 갱신합니다. --next와 --blocker는 기존 목록을 바꿉니다(빠진 항목은 출력에 표시).\n"
                            "기존 다음 할 일에 덧붙이려면 --add-next를 쓰세요. 여러 항목은 ; 로 구분합니다.",
                epilog='예: ctx handoff --done "로그인 구현" --next "결제 연동;배포 체크리스트" --blocker "스테이징 권한 대기"\n'
                       '    ctx handoff --add-next "문서 정리"\n    ctx handoff --clear-blockers')
    p.add_argument("--done", action="append", help="끝낸 일 (다음 할 일 목록에서 비슷한 항목을 지움)")
    p.add_argument("--next", action="append", help="다음 할 일 (기존 목록을 대체)")
    p.add_argument("--add-next", action="store_true", help="--next를 기존 목록 뒤에 덧붙임")
    p.add_argument("--blocker", action="append", help="막힌 일 (기존 목록을 대체)")
    p.add_argument("--clear-blockers", action="store_true", help="막힌 일 비우기")
    p.add_argument("--doing", action="append", help="진행 중인 일 (기존 목록을 대체)")
    p.add_argument("--goal", help="현재 목표")
    p.add_argument("-p", "--project", help="프로젝트 이름·별칭 (생략하면 현재 폴더)")
    p.set_defaults(func=cmd_handoff)

    p = command("now", "NOW.md 보기")
    p.add_argument("project", nargs="?", help="프로젝트 이름·별칭 (생략하면 현재 폴더)")
    p.add_argument("--all", action="store_true", help="모든 프로젝트")
    p.set_defaults(func=cmd_now)

    p = command("projects", "프로젝트 목록과 이름·별칭 관리",
                epilog="예: ctx projects                        목록\n"
                       "    ctx projects add . \"B 프로젝트\"      현재 폴더 등록\n"
                       "    ctx projects alias shop 쇼핑몰 s     별칭 추가\n"
                       "    ctx projects unalias shop s\n"
                       "    ctx projects rename shop \"쇼핑몰 백엔드\"\n"
                       "    ctx projects capture shop off       대화 기록 수집 끄기\n"
                       "    ctx projects remove shop            등록 해제 (지식 파일은 남음)")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "alias", "unalias", "rename", "capture", "remove"], help="작업 (기본: list)")
    p.add_argument("slug", nargs="?", help="프로젝트 slug (add에서는 폴더 경로)")
    p.add_argument("values", nargs="*", help="별칭, 이름, on/off 등")
    p.set_defaults(func=cmd_projects)

    p = command("review", "에이전트·큐레이터가 제안한 지식 검토",
                description="인자 없이 실행하면 하나씩 묻습니다 (y=승인 n=기각 e=편집 후 승인 s=건너뛰기 q=끝내기).",
                epilog="예: ctx review\n    ctx review list\n    ctx review approve rev-1a2b3c dec-4d5e6f\n    ctx review reject --all -p shop")
    p.add_argument("action", nargs="?", choices=["list", "approve", "reject"], help="생략하면 대화형 검토")
    p.add_argument("ids", nargs="*", help="검토 항목 id(rev-…) 또는 지식 id(dec-… 등)")
    p.add_argument("--all", action="store_true", help="대기 중인 항목 전부")
    p.add_argument("-p", "--project", help="이 프로젝트 항목만")
    p.set_defaults(func=cmd_review)

    p = command("curate", "대화 기록을 수집하고 LLM으로 지식화",
                epilog="LLM: ANTHROPIC_API_KEY(+ pip install 'context-hub[llm]') 또는 CTX_CURATOR=claude (Claude Code 구독)")
    p.add_argument("--now", action="store_true", help="방금 끝난 세션도 처리 (기본은 10분 조용해진 뒤)")
    p.add_argument("--no-ingest", action="store_true", help="수집은 건너뛰고 정리만")
    p.add_argument("--quiet", action="store_true", help="출력 없음")
    p.set_defaults(func=cmd_curate)

    p = command("ingest", "대화 기록만 수집")
    p.add_argument("--clients", help="claude-code,codex 중 일부")
    p.add_argument("paths", nargs="*", help="특정 대화 기록 파일만")
    p.set_defaults(func=cmd_ingest)

    command("status", "허브 상태 요약").set_defaults(func=cmd_status)
    command("reindex", "검색 인덱스 재생성").set_defaults(func=cmd_reindex)

    p = command("show", "지식 페이지 보기")
    p.add_argument("id", help="지식 id (recall 결과의 id:…)")
    p.add_argument("--sources", action="store_true", help="증거 원문도 표시")
    p.set_defaults(func=cmd_show)

    p = command("forget", "지식을 보관 처리 (recall에서 빠짐, 증거는 남음)")
    p.add_argument("id", help="지식 id")
    p.set_defaults(func=cmd_forget)

    p = command("sync", "허브를 git 원격과 동기화 (다른 기기와 공유)",
                epilog="예: ctx sync add git@github.com:me/my-context-hub.git   (반드시 비공개 저장소)\n    ctx sync")
    p.add_argument("action", nargs="?", choices=["add"], help="add = 원격 등록")
    p.add_argument("url", nargs="?", help="git 원격 주소")
    p.set_defaults(func=cmd_sync)

    p = command("import-legacy", "기존 context-memory DB 가져오기 (기본은 미리보기)")
    p.add_argument("--db", default=str(Path.home() / ".local/share/context-memory/memory.db"), help="DB 경로")
    p.add_argument("--apply", action="store_true", help="실제로 가져오기")
    p.add_argument("--include-proposed", action="store_true", help="proposed 기억도 가져와 검토 대기에 넣기")
    p.add_argument("--no-merge-by-name", action="store_true", help="같은 이름의 git 아닌 폴더를 합치지 않음")
    p.set_defaults(func=cmd_import)

    p = command("config", "설정 보기·바꾸기 (~/context-hub/config.toml)",
                epilog="예: ctx config curator claude      Claude Code 구독으로 큐레이터 켜기\n"
                       "    ctx config daily-calls 10      하루 큐레이터 호출 상한\n"
                       "    ctx config curator off")
    p.add_argument("key", nargs="?", choices=sorted(CONFIG_KEYS), help="설정 이름 (생략하면 전체 보기)")
    p.add_argument("value", nargs="?", help="새 값 (생략하면 현재 값)")
    p.set_defaults(func=cmd_config)

    p = command("hook", "클라이언트 훅 진입점 (JSON을 표준 입력으로 받음)")
    p.add_argument("action", choices=["start", "prompt", "flush"])
    p.set_defaults(func=cmd_hook)

    command("serve", "MCP stdio 서버 실행 (클라이언트가 띄움)").set_defaults(func=cmd_serve)

    p = command("daemon", "주기적 수집·정리", epilog="install/uninstall은 macOS launchd용입니다.")
    p.add_argument("action", choices=["run", "install", "uninstall"])
    p.add_argument("--interval", type=int, default=900, help="초 단위 간격 (기본 900)")
    p.set_defaults(func=cmd_daemon)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
