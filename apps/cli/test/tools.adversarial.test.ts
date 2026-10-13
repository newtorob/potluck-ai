/**
 * Adversarial tests for filesystem confinement.
 *
 * Companion to tools.confinement.test.ts. That file pins the intended behavior;
 * this one tries to break it. Tests are grouped by attack vector and named for
 * the vector, so a failure names the escape.
 *
 * Threat model: workspace content is attacker-influenced (a cloned repo, a PR
 * branch, a downloaded sample) and the model driving the tools is
 * prompt-injectable. The attacker cannot run commands (run_command is a separate
 * authority boundary, out of scope) and cannot write outside the workspace
 * except through the tools. The question is whether a path argument can reach
 * content outside the configured root, in either direction.
 */
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTool, type Confirm } from "../src/tools.js";

/** The workspace the tools are confined to. */
let cwd = "";
/** A sibling directory that must stay unreachable. */
let outside = "";

const allow: Confirm = async () => true;
const ESCAPES = /escapes the working directory/;
const SECRET = "OUTSIDE-SECRET-42\n";
const INSIDE = "INSIDE-MARKER-7\n";
/** A real NUL byte, built at runtime so the source stays printable. */
const NUL = String.fromCharCode(0);

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-adv-"));
  outside = await mkdtemp(join(tmpdir(), "potluck-advout-"));
  await writeFile(join(outside, "secret.txt"), SECRET, "utf8");
  await mkdir(join(outside, "vault"));
  await writeFile(join(outside, "vault", "secret.txt"), SECRET, "utf8");
});

afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
  await rm(outside, { recursive: true, force: true });
});

/** True when nothing exists at `p` (lstat, so a dangling link counts as present). */
async function absent(p: string): Promise<boolean> {
  try {
    await lstat(p);
    return false;
  } catch {
    return true;
  }
}

// ---------------------------------------------------------------------------
// Vector 1: long symlink chains
// ---------------------------------------------------------------------------

describe("vector 1: symlink chains", () => {
  it("refuses a 4-hop chain that ends outside", async () => {
    await symlink(join(outside, "secret.txt"), join(cwd, "h4.txt"));
    await symlink(join(cwd, "h4.txt"), join(cwd, "h3.txt"));
    await symlink(join(cwd, "h3.txt"), join(cwd, "h2.txt"));
    await symlink(join(cwd, "h2.txt"), join(cwd, "h1.txt"));
    const out = await runTool("read_file", { path: "h1.txt" }, { cwd, confirm: allow });
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("refuses a chain whose middle hops are inside but whose last hop is outside", async () => {
    await mkdir(join(cwd, "a"));
    await mkdir(join(cwd, "b"));
    await symlink(join(outside, "secret.txt"), join(cwd, "b", "tail.txt"));
    await symlink(join(cwd, "b", "tail.txt"), join(cwd, "a", "mid.txt"));
    await symlink(join(cwd, "a", "mid.txt"), join(cwd, "head.txt"));
    const out = await runTool("read_file", { path: "head.txt" }, { cwd, confirm: allow });
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("refuses a chain that leaves the workspace and descends outside", async () => {
    await symlink(outside, join(cwd, "hop"));
    const out = await runTool(
      "read_file",
      { path: "hop/vault/secret.txt" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("allows a 3-hop chain that stays inside", async () => {
    await mkdir(join(cwd, "real"));
    await writeFile(join(cwd, "real", "note.txt"), INSIDE, "utf8");
    await symlink(join(cwd, "real", "note.txt"), join(cwd, "c.txt"));
    await symlink(join(cwd, "c.txt"), join(cwd, "b.txt"));
    await symlink(join(cwd, "b.txt"), join(cwd, "a.txt"));
    const out = await runTool("read_file", { path: "a.txt" }, { cwd, confirm: allow });
    expect(out).toBe(INSIDE);
  });
});

// ---------------------------------------------------------------------------
// Vector 2: symlinked directory in the middle of a path
// ---------------------------------------------------------------------------

describe("vector 2: symlinked directory mid-path", () => {
  it("refuses inside/linkdir/sub/file where linkdir points outside", async () => {
    await mkdir(join(cwd, "inside"));
    await mkdir(join(outside, "vault", "sub"), { recursive: true });
    await writeFile(join(outside, "vault", "sub", "deep.txt"), SECRET, "utf8");
    await symlink(join(outside, "vault"), join(cwd, "inside", "linkdir"));
    const out = await runTool(
      "read_file",
      { path: "inside/linkdir/sub/deep.txt" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("refuses parentlink/other-sibling/secret via a link to the workspace parent", async () => {
    const parent = await realpath(join(cwd, ".."));
    await symlink(parent, join(cwd, "parentlink"));
    const outsideName = (await realpath(outside)).split("/").pop() as string;
    const out = await runTool(
      "read_file",
      { path: `parentlink/${outsideName}/secret.txt` },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("allows parentlink/<workspace>/file, which genuinely lands back inside", async () => {
    const realCwd = await realpath(cwd);
    const parent = await realpath(join(cwd, ".."));
    await symlink(parent, join(cwd, "parentlink"));
    await writeFile(join(cwd, "in.txt"), INSIDE, "utf8");
    const selfName = realCwd.split("/").pop() as string;
    const out = await runTool(
      "read_file",
      { path: `parentlink/${selfName}/in.txt` },
      { cwd, confirm: allow },
    );
    expect(out).toBe(INSIDE);
  });

  it("refuses list_dir and search_files through a mid-path outside link", async () => {
    await mkdir(join(cwd, "inside"));
    await symlink(join(outside, "vault"), join(cwd, "inside", "linkdir"));
    const listed = await runTool(
      "list_dir",
      { path: "inside/linkdir" },
      { cwd, confirm: allow },
    );
    expect(listed).toMatch(ESCAPES);
    const searched = await runTool(
      "search_files",
      { query: "OUTSIDE-SECRET", path: "inside/linkdir" },
      { cwd, confirm: allow },
    );
    expect(searched).toMatch(ESCAPES);
    expect(searched).not.toContain("OUTSIDE-SECRET-42");
  });
});

// ---------------------------------------------------------------------------
// Vector 3: lexical normalization vs kernel resolution disagree
// ---------------------------------------------------------------------------

describe("vector 3: dot-dot after a symlinked directory", () => {
  beforeEach(async () => {
    await symlink(join(outside, "vault"), join(cwd, "linkdir"));
  });

  it("read_file opens the normalized in-workspace path, not the kernel path", async () => {
    // The kernel would resolve linkdir/../secret.txt to OUTSIDE/secret.txt.
    // resolve() collapses it to <cwd>/secret.txt first, and every fs call uses
    // that resolved value rather than the raw argument.
    await writeFile(join(cwd, "secret.txt"), INSIDE, "utf8");
    const out = await runTool(
      "read_file",
      { path: "linkdir/../secret.txt" },
      { cwd, confirm: allow },
    );
    expect(out).toBe(INSIDE);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("write_file through linkdir/.. lands inside, not in the link target's parent", async () => {
    const out = await runTool(
      "write_file",
      { path: "linkdir/../planted.txt", content: "landed\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "planted.txt"), "utf8")).toBe("landed\n");
    expect(await absent(join(outside, "planted.txt"))).toBe(true);
  });

  it("refuses a dot-dot climb that normalizes to a real escape", async () => {
    const outsideName = (await realpath(outside)).split("/").pop() as string;
    const out = await runTool(
      "read_file",
      { path: `linkdir/../../${outsideName}/secret.txt` },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });
});

// ---------------------------------------------------------------------------
// Vector 4: NEW files, where the target does not exist yet
// ---------------------------------------------------------------------------

describe("vector 4: creating files that do not exist yet", () => {
  it("refuses a new file whose nearest existing ancestor is an outside link", async () => {
    await symlink(join(outside, "vault"), join(cwd, "linkdir"));
    const out = await runTool(
      "write_file",
      { path: "linkdir/a/b/c.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(await absent(join(outside, "vault", "a"))).toBe(true);
  });

  it("allows two or more missing components under a real inside directory", async () => {
    const out = await runTool(
      "write_file",
      { path: "new/deep/nest/file.txt", content: "ok\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "new", "deep", "nest", "file.txt"), "utf8")).toBe("ok\n");
  });

  it("allows a missing file whose PARENT is a symlink pointing back INTO the workspace", async () => {
    await mkdir(join(cwd, "real"));
    await symlink(join(cwd, "real"), join(cwd, "aliasdir"));
    const out = await runTool(
      "write_file",
      { path: "aliasdir/fresh.txt", content: "ok\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "real", "fresh.txt"), "utf8")).toBe("ok\n");
  });

  it("fails safely when an intermediate component exists as a FILE, not a directory", async () => {
    await writeFile(join(cwd, "notadir.txt"), "x\n", "utf8");
    const out = await runTool(
      "write_file",
      { path: "notadir.txt/child.txt", content: "ok\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/^error: /);
    expect(out).not.toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "notadir.txt"), "utf8")).toBe("x\n");
  });

  // --- the escape -------------------------------------------------------
  it("ESCAPE: write_file through a DANGLING symlink creates a file outside the workspace", async () => {
    // realpath() fails on a dangling symlink, so canonicalize() falls back to
    // the nearest existing ancestor (the workspace root) and re-appends the
    // link's own name. Containment is then judged on <root>/notes.md, which is
    // inside. open(..., "w") follows the link and creates the outside target.
    const target = join(outside, "vault", "planted.txt");
    await symlink(target, join(cwd, "notes.md"));
    const subjects: string[] = [];
    const confirm = vi.fn<Confirm>(async (ctx) => {
      subjects.push(ctx.subject);
      return true;
    });
    const out = await runTool(
      "write_file",
      { path: "notes.md", content: "PWNED-BY-DANGLING-LINK\n" },
      { cwd, confirm },
    );

    // Evidence, printed before the assertions so a failing run carries it.
    const landed = await absent(target)
      ? "(nothing outside)"
      : JSON.stringify(await readFile(target, "utf8"));
    console.log(
      [
        "DANGLING-LINK ESCAPE",
        `  workspace root : ${cwd}`,
        `  tool returned  : ${out}`,
        `  gate subject   : ${subjects.join(", ") || "(gate never called)"}`,
        `  outside target : ${target}`,
        `  target content : ${landed}`,
      ].join("\n"),
    );

    expect(await absent(target)).toBe(true);
    expect(out).toMatch(ESCAPES);
    // The human gate is shown the innocuous relative path, not the real target.
    expect(confirm).not.toHaveBeenCalled();
  });

  it("ESCAPE: the same trick via a dangling chain whose hops run outside", async () => {
    const target = join(outside, "vault", "chained.txt");
    await symlink(target, join(outside, "hop.txt"));
    await symlink(join(outside, "hop.txt"), join(cwd, "doc.md"));
    const out = await runTool(
      "write_file",
      { path: "doc.md", content: "PWNED-VIA-CHAIN\n" },
      { cwd, confirm: allow },
    );
    const landed = await absent(target)
      ? "(nothing outside)"
      : JSON.stringify(await readFile(target, "utf8"));
    console.log(`DANGLING-CHAIN ESCAPE\n  tool returned  : ${out}\n  target content : ${landed}`);
    expect(await absent(target)).toBe(true);
    expect(out).toMatch(ESCAPES);
  });

  it("a dangling symlink to an outside DIRECTORY does not let a write land", async () => {
    // Recursive mkdir refuses to build through the dangling link, so the write
    // fails before it can plant anything. Non-escape, recorded for contrast.
    await symlink(join(outside, "brandnew"), join(cwd, "dirlink"));
    const out = await runTool(
      "write_file",
      { path: "dirlink/x.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).not.toMatch(/wrote \d+ bytes/);
    expect(await absent(join(outside, "brandnew"))).toBe(true);
  });

  it("read_file and edit_file cannot use the dangling-link trick", async () => {
    await symlink(join(outside, "vault", "nothere.txt"), join(cwd, "notes.md"));
    const read = await runTool("read_file", { path: "notes.md" }, { cwd, confirm: allow });
    expect(read).toMatch(/^error: /);
    const edited = await runTool(
      "edit_file",
      { path: "notes.md", old_string: "a", new_string: "b" },
      { cwd, confirm: allow },
    );
    expect(edited).toMatch(/^error: /);
    expect(await absent(join(outside, "vault", "nothere.txt"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Vector 5: edge-case path inputs
// ---------------------------------------------------------------------------

describe("vector 5: edge-case inputs", () => {
  it("handles the empty string, dot, dot-slash and a trailing slash without escaping", async () => {
    await writeFile(join(cwd, "a.txt"), INSIDE, "utf8");
    await mkdir(join(cwd, "sub"));
    for (const p of ["", ".", "./"]) {
      const listed = await runTool("list_dir", { path: p }, { cwd, confirm: allow });
      expect(listed.split("\n")).toContain("a.txt");
    }
    const trailing = await runTool("list_dir", { path: "sub/" }, { cwd, confirm: allow });
    expect(trailing).toBe("(empty directory)");
    const readRoot = await runTool("read_file", { path: "." }, { cwd, confirm: allow });
    expect(readRoot).toMatch(/^error: /);
  });

  it("treats the workspace root itself as inside, and its parent as outside", async () => {
    const listed = await runTool("list_dir", { path: cwd }, { cwd, confirm: allow });
    expect(listed).not.toMatch(ESCAPES);
    const up = await runTool("list_dir", { path: join(cwd, "..") }, { cwd, confirm: allow });
    expect(up).toMatch(ESCAPES);
  });

  it("refuses the filesystem root and other absolute paths", async () => {
    for (const p of ["/", "/etc", "/etc/passwd", "/../etc/passwd"]) {
      const out = await runTool("read_file", { path: p }, { cwd, confirm: allow });
      expect(out).toMatch(ESCAPES);
      expect(out).not.toContain("root:");
    }
  });

  it("does NOT expand a leading tilde (it is a literal directory name)", async () => {
    await mkdir(join(cwd, "~"));
    await writeFile(join(cwd, "~", "tilde.txt"), INSIDE, "utf8");
    const out = await runTool("read_file", { path: "~/tilde.txt" }, { cwd, confirm: allow });
    expect(out).toBe(INSIDE);
    const home = await runTool("read_file", { path: "~/.ssh/id_rsa" }, { cwd, confirm: allow });
    expect(home).toMatch(/^error: /);
    expect(home).not.toContain("PRIVATE KEY");
  });

  it("rejects a NUL byte without crashing and without escaping", async () => {
    const out = await runTool("read_file", { path: `a${NUL}b` }, { cwd, confirm: allow });
    expect(out).toMatch(/^error: /);
    expect(out).not.toContain("OUTSIDE-SECRET");
    const sneaky = await runTool(
      "read_file",
      { path: `${join(outside, "secret.txt")}${NUL}.txt` },
      { cwd, confirm: allow },
    );
    expect(sneaky).toMatch(/^error: /);
    expect(sneaky).not.toContain("OUTSIDE-SECRET");
  });

  it("survives a very long path", async () => {
    const long = Array.from({ length: 400 }, (_, i) => `seg${i}xxxxxxxxxxxxxxxx`).join("/");
    const out = await runTool("read_file", { path: long }, { cwd, confirm: allow });
    expect(out).toMatch(/^error: /);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("treats Windows separators as a literal filename on POSIX", async () => {
    const weird = "..\\..\\etc\\passwd";
    await writeFile(join(cwd, weird), INSIDE, "utf8");
    const out = await runTool("read_file", { path: weird }, { cwd, confirm: allow });
    expect(out).toBe(INSIDE);
    expect(out).not.toContain("root:");
  });

  it("refuses non-string and missing path arguments", async () => {
    const num = await runTool("read_file", { path: 42 }, { cwd, confirm: allow });
    expect(num).toMatch(/must be a string/);
    const none = await runTool("read_file", {}, { cwd, confirm: allow });
    expect(none).toMatch(/must be a string/);
  });
});

// ---------------------------------------------------------------------------
// Vector 6: sibling-prefix trap
// ---------------------------------------------------------------------------

describe("vector 6: sibling directories sharing the workspace name prefix", () => {
  const siblings = ["2", ".bak", "-old"];

  it("refuses each sibling by absolute path", async () => {
    for (const suffix of siblings) {
      const sib = `${cwd}${suffix}`;
      await mkdir(sib, { recursive: true });
      try {
        await writeFile(join(sib, "secret.txt"), SECRET, "utf8");
        const out = await runTool(
          "read_file",
          { path: join(sib, "secret.txt") },
          { cwd, confirm: allow },
        );
        expect(out).toMatch(ESCAPES);
        expect(out).not.toContain("OUTSIDE-SECRET");
      } finally {
        await rm(sib, { recursive: true, force: true });
      }
    }
  });

  it("refuses each sibling through a symlink inside the workspace", async () => {
    for (const suffix of siblings) {
      const sib = `${cwd}${suffix}`;
      await mkdir(sib, { recursive: true });
      try {
        await writeFile(join(sib, "secret.txt"), SECRET, "utf8");
        const linkName = `sib${suffix.replace(/\W/g, "")}`;
        await symlink(sib, join(cwd, linkName));
        const out = await runTool(
          "read_file",
          { path: `${linkName}/secret.txt` },
          { cwd, confirm: allow },
        );
        expect(out).toMatch(ESCAPES);
        expect(out).not.toContain("OUTSIDE-SECRET");
        const listed = await runTool("list_dir", { path: linkName }, { cwd, confirm: allow });
        expect(listed).toMatch(ESCAPES);
      } finally {
        await rm(sib, { recursive: true, force: true });
      }
    }
  });

  it("refuses a write into a prefix sibling", async () => {
    const sib = `${cwd}2`;
    await mkdir(sib, { recursive: true });
    try {
      const out = await runTool(
        "write_file",
        { path: join(sib, "planted.txt"), content: "pwned\n" },
        { cwd, confirm: allow },
      );
      expect(out).toMatch(ESCAPES);
      expect(await absent(join(sib, "planted.txt"))).toBe(true);
    } finally {
      await rm(sib, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// Vector 7: the workspace root is itself a symlink
// ---------------------------------------------------------------------------

describe("vector 7: symlinked workspace root", () => {
  let realRoot = "";
  let linkRoot = "";

  beforeEach(async () => {
    realRoot = await mkdtemp(join(tmpdir(), "potluck-advreal-"));
    linkRoot = join(outside, "ws-link");
    await symlink(realRoot, linkRoot);
    await writeFile(join(realRoot, "a.txt"), INSIDE, "utf8");
  });

  afterEach(async () => {
    await rm(realRoot, { recursive: true, force: true });
  });

  it("every tool works normally through the link root", async () => {
    expect(await runTool("read_file", { path: "a.txt" }, { cwd: linkRoot, confirm: allow })).toBe(
      INSIDE,
    );
    expect(
      await runTool("list_dir", { path: "." }, { cwd: linkRoot, confirm: allow }),
    ).toContain("a.txt");
    expect(
      await runTool("search_files", { query: "INSIDE-MARKER" }, { cwd: linkRoot, confirm: allow }),
    ).toContain("a.txt");
    expect(
      await runTool("glob", { pattern: "**/*.txt" }, { cwd: linkRoot, confirm: allow }),
    ).toContain("a.txt");
    expect(
      await runTool(
        "edit_file",
        { path: "a.txt", old_string: "INSIDE-MARKER-7", new_string: "EDITED" },
        { cwd: linkRoot, confirm: allow },
      ),
    ).toMatch(/edited a\.txt/);
    expect(
      await runTool(
        "write_file",
        { path: "sub/new.txt", content: "ok\n" },
        { cwd: linkRoot, confirm: allow },
      ),
    ).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(realRoot, "sub", "new.txt"), "utf8")).toBe("ok\n");
  });

  it("still refuses outside targets addressed through the LINK path", async () => {
    // linkRoot's lexical parent is the outside directory, not its canonical parent.
    const out = await runTool(
      "read_file",
      { path: join(linkRoot, "..", "secret.txt") },
      { cwd: linkRoot, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
    const rel = await runTool(
      "read_file",
      { path: "../secret.txt" },
      { cwd: linkRoot, confirm: allow },
    );
    expect(rel).toMatch(ESCAPES);
    expect(rel).not.toContain("OUTSIDE-SECRET");
  });

  it("still refuses outside targets addressed through the REAL path", async () => {
    const out = await runTool(
      "read_file",
      { path: join(outside, "vault", "secret.txt") },
      { cwd: linkRoot, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("accepts an absolute REAL-root path while cwd is the link", async () => {
    const out = await runTool(
      "read_file",
      { path: join(realRoot, "a.txt") },
      { cwd: linkRoot, confirm: allow },
    );
    expect(out).toBe(INSIDE);
  });
});

// ---------------------------------------------------------------------------
// Vector 8: the walker, and other modules that read files
// ---------------------------------------------------------------------------

describe("vector 8: walker behavior and other file readers", () => {
  it("search_files and glob never descend a symlinked directory pointing outside", async () => {
    await mkdir(join(cwd, "nest"));
    await symlink(join(outside, "vault"), join(cwd, "nest", "linkdir"));
    await writeFile(join(cwd, "nest", "in.txt"), "OUTSIDE-SECRET decoy\n", "utf8");
    const searched = await runTool(
      "search_files",
      { query: "OUTSIDE-SECRET" },
      { cwd, confirm: allow },
    );
    expect(searched).toContain("nest/in.txt");
    expect(searched).not.toContain("linkdir");
    const globbed = await runTool("glob", { pattern: "**/*.txt" }, { cwd, confirm: allow });
    expect(globbed).toContain("nest/in.txt");
    expect(globbed).not.toContain("linkdir");
  });

  it("TRADE-OFF: the walker also skips a symlinked file that points INSIDE", async () => {
    // Documented, not a bug: search_files and glob under-report in-workspace
    // aliases. The real target is still found under its real name, and read_file
    // on the alias still works, so the alias is reachable, just not listed.
    await mkdir(join(cwd, "real"));
    await writeFile(join(cwd, "real", "note.txt"), INSIDE, "utf8");
    await symlink(join(cwd, "real", "note.txt"), join(cwd, "alias.txt"));
    await symlink(join(cwd, "real"), join(cwd, "aliasdir"));
    const globbed = await runTool("glob", { pattern: "**/*.txt" }, { cwd, confirm: allow });
    expect(globbed).toContain("real/note.txt");
    expect(globbed).not.toContain("alias.txt");
    expect(globbed).not.toContain("aliasdir");
    const searched = await runTool(
      "search_files",
      { query: "INSIDE-MARKER" },
      { cwd, confirm: allow },
    );
    expect(searched.split("\n").filter((l) => l.length > 0)).toHaveLength(1);
    expect(await runTool("read_file", { path: "alias.txt" }, { cwd, confirm: allow })).toBe(
      INSIDE,
    );
  });

  it("the repo map never lists a path reached through a symlink", async () => {
    const { buildRepoMap } = await import("../src/repomap.js");
    await symlink(join(outside, "vault"), join(cwd, "linkdir"));
    await symlink(join(outside, "secret.txt"), join(cwd, "linkfile.txt"));
    await writeFile(join(cwd, "ok.txt"), INSIDE, "utf8");
    const map = await buildRepoMap(cwd);
    expect(map).toContain("ok.txt");
    expect(map).not.toContain("linkdir");
    expect(map).not.toContain("linkfile.txt");
    expect(map).not.toContain("OUTSIDE-SECRET");
  });

  it("INFORMATIONAL: loadIgnore reads .gitignore through a symlink pointing outside", async () => {
    // walk.loadIgnore does readFile(join(root, ".gitignore")) with no
    // confinement check, so an out-of-workspace file decides which in-workspace
    // files the agent is shown. No outside content reaches the model, so this is
    // control-flow influence rather than a disclosure.
    await writeFile(join(outside, "rules"), "hidden.txt\n", "utf8");
    await symlink(join(outside, "rules"), join(cwd, ".gitignore"));
    await writeFile(join(cwd, "hidden.txt"), INSIDE, "utf8");
    await writeFile(join(cwd, "shown.txt"), INSIDE, "utf8");
    const globbed = await runTool("glob", { pattern: "**/*.txt" }, { cwd, confirm: allow });
    expect(globbed).toContain("shown.txt");
    expect(globbed).not.toContain("hidden.txt"); // the outside rules took effect
  });
});

// ---------------------------------------------------------------------------
// Vector 9: case-insensitive filesystems
// ---------------------------------------------------------------------------

describe("vector 9: case-insensitive filesystem", () => {
  it("containment holds when the workspace root is passed in a different case", async () => {
    const upper = cwd.toUpperCase();
    let caseInsensitive = false;
    try {
      caseInsensitive = (await realpath(upper)) === (await realpath(cwd));
    } catch {
      caseInsensitive = false;
    }

    await writeFile(join(cwd, "inside.txt"), INSIDE, "utf8");

    const escape = await runTool(
      "read_file",
      { path: join(outside, "secret.txt") },
      { cwd: upper, confirm: allow },
    );
    expect(escape).toMatch(/^error: /);
    expect(escape).not.toContain("OUTSIDE-SECRET");

    const sibling = `${cwd}2`;
    await mkdir(sibling, { recursive: true });
    try {
      await writeFile(join(sibling, "secret.txt"), SECRET, "utf8");
      const sib = await runTool(
        "read_file",
        { path: join(sibling, "secret.txt").toUpperCase() },
        { cwd: upper, confirm: allow },
      );
      expect(sib).toMatch(/^error: /);
      expect(sib).not.toContain("OUTSIDE-SECRET");
    } finally {
      await rm(sibling, { recursive: true, force: true });
    }

    if (caseInsensitive) {
      // realpath normalizes case on macOS, so in-workspace access still works.
      const ok = await runTool("read_file", { path: "inside.txt" }, { cwd: upper, confirm: allow });
      expect(ok).toBe(INSIDE);
    }
  });
});

// ---------------------------------------------------------------------------
// Vector 10: the accepted time-of-check / time-of-use race
// ---------------------------------------------------------------------------

describe("vector 10: TOCTOU is documented as accepted", () => {
  it("resolveInCwd carries an explicit residual-race note", async () => {
    const src = await readFile(
      fileURLToPath(new URL("../src/tools.ts", import.meta.url)),
      "utf8",
    );
    expect(src).toMatch(/Residual race/);
    expect(src).toMatch(/check-then-use/);
    expect(src).toMatch(/swapped between this check/);
  });
});
