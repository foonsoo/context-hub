"""`ctx` command line."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, adapters, curator, hooks, knowledge, legacy, ops, review, setup
from .hub import Hub, Project, default_root, slugify
from .identity import canonical_path, git_info, project_for_cwd, resolve
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
    if legacy_db.exists():
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
    result = ops.note(hub, text, cwd=os.getcwd(), kind=args.kind, project=args.project, client="cli")
    print(result.message)
    return 0 if result.ok else 1


def cmd_handoff(args) -> int:
    hub = _require(_hub(args))
    result = ops.handoff(hub, cwd=os.getcwd(), done=_lines(args.done), next_steps=_lines(args.next),
                         blockers=_lines(args.blocker) if args.blocker is not None or args.clear_blockers else None,
                         goal=args.goal, in_progress=_lines(args.doing), project=args.project, client="cli")
    print(result.message)
    return 0 if result.ok else 1


def cmd_now(args) -> int:
    hub = _require(_hub(args))
    projects = hub.projects()
    if args.all:
        slugs = sorted(projects)
    else:
        res = resolve(hub, cwd=os.getcwd(), text=args.project or "", project=args.project)
        if not res.slugs:
            print("현재 폴더의 프로젝트를 찾지 못했습니다. `ctx now <프로젝트>` 또는 `ctx now --all`")
            return 1
        slugs = res.slugs
    for slug in slugs:
        doc = knowledge.load_now(hub, slug, projects[slug].display if slug in projects else slug)
        print(doc.text().strip() if not doc.is_empty() else f"# NOW · {doc.title}\n(아직 기록 없음)")
        print()
    return 0


def cmd_projects(args) -> int:
    hub = _require(_hub(args))
    projects = hub.projects()
    if args.action == "alias":
        if args.slug not in projects:
            sys.exit(f"프로젝트 없음: {args.slug}")
        aliases = projects[args.slug].aliases + [a for a in args.values if a not in projects[args.slug].aliases]
        hub.update_project(args.slug, aliases=aliases)
        hub.commit(f"alias: {args.slug}")
        print(f"{args.slug} 별칭: {', '.join(aliases)}")
        return 0
    if args.action == "rename":
        if args.slug not in projects or not args.values:
            sys.exit("사용법: ctx projects rename <slug> <표시 이름>")
        hub.update_project(args.slug, name=" ".join(args.values))
        hub.commit(f"rename: {args.slug}")
        return 0
    if args.action == "capture":
        if args.slug not in projects or not args.values or args.values[0] not in ("on", "off"):
            sys.exit("사용법: ctx projects capture <slug> on|off")
        hub.update_project(args.slug, capture=args.values[0] == "on")
        hub.commit(f"capture {args.values[0]}: {args.slug}")
        return 0
    if args.action == "add":
        path = canonical_path(args.slug or os.getcwd())
        info = git_info(path)
        name = " ".join(args.values) if args.values else Path(path).name
        project = hub.add_project(Project(
            slug=slugify(name), name=name,
            remotes=[info.remote] if info and info.remote else [],
            paths=[info.common_root if info else path],
        ))
        hub.commit(f"project: {project.slug}")
        print(f"등록: {project.slug} ({project.display})")
        return 0
    here, how = project_for_cwd(hub, os.getcwd(), projects)
    if not projects:
        print("등록된 프로젝트가 없습니다. 첫 note/handoff 때 자동으로 등록됩니다.")
    for project in projects.values():
        mark = "*" if project.slug == here else " "
        extra = f" 별칭: {', '.join(project.aliases)}" if project.aliases else ""
        capture = "" if project.capture else " (수집 꺼짐)"
        print(f"{mark} {project.slug:<24} {project.display}{extra}{capture}")
    return 0


def cmd_review(args) -> int:
    hub = _require(_hub(args))
    items = review.pending(hub, project=args.project)
    if args.action == "list" or (args.action is None and not sys.stdin.isatty()):
        if not items:
            print("검토할 항목이 없습니다.")
        for item in items:
            print(review.describe(hub, item))
            print()
        return 0
    if args.action in ("approve", "reject"):
        targets = items if args.all else [i for i in (review.get(hub, x) for x in args.ids) if i]
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
    changed = 0
    for item in items:
        print(review.describe(hub, item))
        while True:
            try:
                answer = input("  [y/n/e/s/q] > ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in ("y", "n", "e", "s", "q", ""):
                break
        if answer == "q":
            break
        if answer == "y":
            print("  " + review.approve(hub, item))
            changed += 1
        elif answer == "n":
            print("  " + review.reject(hub, item))
            changed += 1
        elif answer == "e":
            print("  " + review.edit_then_approve(hub, item))
            changed += 1
        print()
    if changed:
        hub.commit(f"review: {changed} items")
    return 0


def cmd_curate(args) -> int:
    hub = _require(_hub(args))
    quiet = 0 if args.now else curator.QUIET_SECONDS
    if not args.no_ingest:
        adapters.ingest(hub, quiet_seconds=0)
    extractor = curator.default_extractor()
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
    extractor = "사용 가능" if curator.default_extractor() else "꺼짐 (pip install 'context-hub[llm]' + ANTHROPIC_API_KEY)"
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
    page.meta["status"] = "archived"
    knowledge.save(hub, page, by_curator=not page.human_edited())
    review.forget_in_now(hub, page)
    hub.commit(f"forget: {page.title}")
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
    return _import(hub, Path(args.db).expanduser(), apply=args.apply, include_proposed=args.include_proposed)


def _import(hub: Hub, db: Path, apply: bool, include_proposed: bool) -> int:
    if not db.exists():
        print(f"DB가 없습니다: {db}")
        return 1
    statuses = ("active", "proposed") if include_proposed else ("active",)
    the_plan = legacy.plan(hub, db, statuses)
    print(the_plan.describe())
    if not apply:
        print("\n미리보기입니다. 적용하려면 --apply를 붙이세요." + ("" if include_proposed else " (proposed까지: --include-proposed)"))
        return 0
    count = legacy.apply(hub, db, the_plan)
    print(f"\n{count}건을 가져왔습니다.")
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
                curator.curate(hub, curator.default_extractor())
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

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctx", description="AI 클라이언트 간 맥락 공유와 지식화")
    parser.add_argument("--version", action="version", version=f"ctx {__version__}")
    parser.add_argument("--home", help=f"허브 위치 (기본: $CTX_HOME 또는 {default_root()})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="허브를 만들고 클라이언트 등록 계획을 보여 줍니다")
    p.add_argument("--register", action="store_true", help="MCP·규칙·훅을 실제로 등록합니다")
    p.add_argument("--clients", help="claude-code,codex,cursor 중 일부")
    p.add_argument("--no-hooks", action="store_true")
    p.add_argument("--import-legacy", metavar="DB", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("uninstall", help="클라이언트 등록·규칙·훅을 제거합니다 (지식은 유지)")
    p.add_argument("--clients")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("recall", help="맥락 조회")
    p.add_argument("query", nargs="+")
    p.add_argument("-p", "--project")
    p.add_argument("-b", "--budget", type=int, default=2000)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("note", help="지식 한 건 기록 (proposed)")
    p.add_argument("text", nargs="*")
    p.add_argument("-k", "--kind", choices=sorted(knowledge.TYPES))
    p.add_argument("-p", "--project")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("handoff", help="NOW.md 갱신 (여러 항목은 ; 로 구분)")
    p.add_argument("--done", action="append")
    p.add_argument("--next", action="append")
    p.add_argument("--blocker", action="append")
    p.add_argument("--clear-blockers", action="store_true")
    p.add_argument("--doing", action="append")
    p.add_argument("--goal")
    p.add_argument("-p", "--project")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("now", help="NOW.md 보기")
    p.add_argument("project", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_now)

    p = sub.add_parser("projects", help="프로젝트 목록·별칭 관리")
    p.add_argument("action", nargs="?", choices=["list", "alias", "rename", "capture", "add"], default="list")
    p.add_argument("slug", nargs="?")
    p.add_argument("values", nargs="*")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("review", help="큐레이터 제안 검토")
    p.add_argument("action", nargs="?", choices=["list", "approve", "reject"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("-p", "--project")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("curate", help="대화 기록 수집 후 지식화")
    p.add_argument("--now", action="store_true", help="방금 끝난 세션도 처리")
    p.add_argument("--no-ingest", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_curate)

    p = sub.add_parser("ingest", help="대화 기록만 수집")
    p.add_argument("--clients")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_ingest)

    sub.add_parser("status", help="상태 요약").set_defaults(func=cmd_status)
    sub.add_parser("reindex", help="인덱스 재생성").set_defaults(func=cmd_reindex)

    p = sub.add_parser("show", help="지식 페이지 보기")
    p.add_argument("id")
    p.add_argument("--sources", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("forget", help="지식 보관 처리")
    p.add_argument("id")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("sync", help="git 원격과 동기화")
    p.add_argument("action", nargs="?", choices=["add"])
    p.add_argument("url", nargs="?")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("import-legacy", help="기존 context-memory DB 가져오기")
    p.add_argument("--db", default=str(Path.home() / ".local/share/context-memory/memory.db"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--include-proposed", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("hook", help="클라이언트 훅 진입점")
    p.add_argument("action", choices=["start", "prompt", "flush"])
    p.set_defaults(func=cmd_hook)

    sub.add_parser("serve", help="MCP stdio 서버").set_defaults(func=cmd_serve)

    p = sub.add_parser("daemon", help="주기적 수집·정리")
    p.add_argument("action", choices=["run", "install", "uninstall"])
    p.add_argument("--interval", type=int, default=900)
    p.set_defaults(func=cmd_daemon)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
