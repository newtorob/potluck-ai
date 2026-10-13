/**
 * Filesystem-confinement tests.
 *
 * A lexical resolveInCwd() check alone lets a path that looks inside the workspace be
 * a symlink (or sit under a symlinked directory) that resolves outside it, and
 * the fs calls after the check follow symlinks. These tests pin the canonical
 * (realpath-based) containment check, and pin that the walker does not cross a
 * symlink into outside content.
 */
import { lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTool, type Confirm } from "../src/tools.js";

/** The workspace the tools are confined to. */
let cwd = "";
/** A sibling directory that must stay unreachable. */
let outside = "";

const allow: Confirm = async () => true;
const ESCAPES = /escapes the working directory/;
const SECRET = "OUTSIDE-SECRET-42\n";

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-conf-"));
  outside = await mkdtemp(join(tmpdir(), "potluck-outside-"));
  await writeFile(join(outside, "secret.txt"), SECRET, "utf8");
  await mkdir(join(outside, "vault"));
  await writeFile(join(outside, "vault", "secret.txt"), SECRET, "utf8");
});

afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
  await rm(outside, { recursive: true, force: true });
});

describe("symlinked FILE pointing outside the workspace", () => {
  it("read_file refuses it (and never returns the outside content)", async () => {
    await symlink(join(outside, "secret.txt"), join(cwd, "link.txt"));
    const confirm = vi.fn<Confirm>(allow);
    const out = await runTool("read_file", { path: "link.txt" }, { cwd, confirm });
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("edit_file refuses it and leaves the outside file untouched", async () => {
    await symlink(join(outside, "secret.txt"), join(cwd, "link.txt"));
    const out = await runTool(
      "edit_file",
      { path: "link.txt", old_string: "OUTSIDE-SECRET-42", new_string: "pwned" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(await readFile(join(outside, "secret.txt"), "utf8")).toBe(SECRET);
  });

  it("write_file refuses it and leaves the outside file untouched", async () => {
    await symlink(join(outside, "secret.txt"), join(cwd, "link.txt"));
    const out = await runTool(
      "write_file",
      { path: "link.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(await readFile(join(outside, "secret.txt"), "utf8")).toBe(SECRET);
  });

  it("refuses a symlink CHAIN (link -> link -> outside)", async () => {
    await symlink(join(outside, "secret.txt"), join(cwd, "hop2.txt"));
    await symlink(join(cwd, "hop2.txt"), join(cwd, "hop1.txt"));
    const out = await runTool("read_file", { path: "hop1.txt" }, { cwd, confirm: allow });
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });
});

describe("symlinked DIRECTORY pointing outside the workspace", () => {
  beforeEach(async () => {
    await symlink(join(outside, "vault"), join(cwd, "linkdir"));
  });

  it("read_file through it is refused", async () => {
    const out = await runTool(
      "read_file",
      { path: "linkdir/secret.txt" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("list_dir on it is refused", async () => {
    const out = await runTool("list_dir", { path: "linkdir" }, { cwd, confirm: allow });
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("secret.txt");
  });

  it("edit_file through it is refused", async () => {
    const out = await runTool(
      "edit_file",
      { path: "linkdir/secret.txt", old_string: "OUTSIDE-SECRET-42", new_string: "x" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(await readFile(join(outside, "vault", "secret.txt"), "utf8")).toBe(SECRET);
  });

  it("write_file over an existing file through it is refused", async () => {
    const out = await runTool(
      "write_file",
      { path: "linkdir/secret.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(await readFile(join(outside, "vault", "secret.txt"), "utf8")).toBe(SECRET);
  });

  it("writing a NEW file under it is refused (nothing lands outside)", async () => {
    const out = await runTool(
      "write_file",
      { path: "linkdir/planted.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    await expect(readFile(join(outside, "vault", "planted.txt"), "utf8")).rejects.toBeTruthy();
  });

  it("writing a NEW file under a NEW subdir of it is refused", async () => {
    const out = await runTool(
      "write_file",
      { path: "linkdir/deep/planted.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    await expect(
      readFile(join(outside, "vault", "deep", "planted.txt"), "utf8"),
    ).rejects.toBeTruthy();
  });

  it("search_files rooted at it is refused", async () => {
    const out = await runTool(
      "search_files",
      { query: "OUTSIDE-SECRET", path: "linkdir" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET-42");
  });

  it("the walker does not descend into it from the workspace root", async () => {
    await writeFile(join(cwd, "inside.txt"), "OUTSIDE-SECRET marker\n", "utf8");
    const out = await runTool(
      "search_files",
      { query: "OUTSIDE-SECRET" },
      { cwd, confirm: allow },
    );
    expect(out).toContain("inside.txt");
    expect(out).not.toContain("linkdir");
  });

  it("glob does not list files reached through it", async () => {
    const out = await runTool("glob", { pattern: "**/*.txt" }, { cwd, confirm: allow });
    expect(out).not.toContain("linkdir");
  });
});

describe("paths that must keep working", () => {
  it("allows a symlink that resolves INSIDE the workspace", async () => {
    await mkdir(join(cwd, "real"));
    await writeFile(join(cwd, "real", "note.txt"), "inside content\n", "utf8");
    await symlink(join(cwd, "real", "note.txt"), join(cwd, "alias.txt"));
    const out = await runTool("read_file", { path: "alias.txt" }, { cwd, confirm: allow });
    expect(out).toBe("inside content\n");
  });

  it("allows a symlinked DIRECTORY that resolves inside the workspace", async () => {
    await mkdir(join(cwd, "real"));
    await writeFile(join(cwd, "real", "note.txt"), "inside content\n", "utf8");
    await symlink(join(cwd, "real"), join(cwd, "aliasdir"));
    const out = await runTool(
      "read_file",
      { path: "aliasdir/note.txt" },
      { cwd, confirm: allow },
    );
    expect(out).toBe("inside content\n");
  });

  it("allows an ABSOLUTE path inside the workspace", async () => {
    await writeFile(join(cwd, "abs.txt"), "abs ok\n", "utf8");
    const out = await runTool(
      "read_file",
      { path: join(cwd, "abs.txt") },
      { cwd, confirm: allow },
    );
    expect(out).toBe("abs ok\n");
  });

  it("reads and writes normally when the workspace ROOT is itself a symlink", async () => {
    const realRoot = await mkdtemp(join(tmpdir(), "potluck-realroot-"));
    const linkRoot = join(outside, "ws-link");
    await symlink(realRoot, linkRoot);
    try {
      await writeFile(join(realRoot, "a.txt"), "root ok\n", "utf8");
      const read = await runTool(
        "read_file",
        { path: "a.txt" },
        { cwd: linkRoot, confirm: allow },
      );
      expect(read).toBe("root ok\n");

      const wrote = await runTool(
        "write_file",
        { path: "sub/b.txt", content: "written\n" },
        { cwd: linkRoot, confirm: allow },
      );
      expect(wrote).toMatch(/wrote \d+ bytes/);
      expect(await readFile(join(realRoot, "sub", "b.txt"), "utf8")).toBe("written\n");

      const listed = await runTool("list_dir", { path: "." }, { cwd: linkRoot, confirm: allow });
      expect(listed.split("\n")).toContain("a.txt");
    } finally {
      await rm(realRoot, { recursive: true, force: true });
    }
  });
});

describe("DANGLING symlinks (target does not exist yet)", () => {
  // realpath() reports ENOENT for a genuinely missing component and for a
  // dangling symlink. Treating the second as "missing" would judge containment on the
  // link's own path while open(..., "w") follows the link and creates the file
  // at its target; these pin both directions of that distinction.

  it("allows a dangling link whose target is INSIDE, and the write lands inside", async () => {
    await symlink("./missing-inside.txt", join(cwd, "notes.md"));
    const out = await runTool(
      "write_file",
      { path: "notes.md", content: "landed inside\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "missing-inside.txt"), "utf8")).toBe("landed inside\n");
  });

  it("resolves a RELATIVE dangling target against the directory holding the link", async () => {
    await mkdir(join(cwd, "sub"));
    // "../fresh.txt" from <cwd>/sub is <cwd>/fresh.txt, still inside.
    await symlink("../fresh.txt", join(cwd, "sub", "notes.md"));
    const out = await runTool(
      "write_file",
      { path: "sub/notes.md", content: "landed\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "fresh.txt"), "utf8")).toBe("landed\n");
  });

  it("refuses a dangling link whose target is OUTSIDE and creates nothing there", async () => {
    const target = join(outside, "vault", "planted.txt");
    await symlink(target, join(cwd, "notes.md"));
    const confirm = vi.fn<Confirm>(allow);
    const out = await runTool(
      "write_file",
      { path: "notes.md", content: "pwned\n" },
      { cwd, confirm },
    );
    expect(out).toMatch(ESCAPES);
    // The refusal names the argument, never the resolved target.
    expect(out).not.toContain(outside);
    expect(confirm).not.toHaveBeenCalled();
    await expect(readFile(target, "utf8")).rejects.toBeTruthy();
  });

  it("refuses a dangling link whose target is a RELATIVE climb out of the workspace", async () => {
    await symlink("../escaped.txt", join(cwd, "notes.md"));
    const out = await runTool(
      "write_file",
      { path: "notes.md", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
  });

  it("refuses a dangling DIRECTORY link used as a mid-path component", async () => {
    await symlink(join(outside, "brandnew"), join(cwd, "dirlink"));
    const out = await runTool(
      "write_file",
      { path: "dirlink/x.txt", content: "pwned\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    await expect(lstat(join(outside, "brandnew"))).rejects.toBeTruthy();
  });

  it("terminates on a symlink CYCLE instead of looping forever", async () => {
    await symlink(join(cwd, "b.txt"), join(cwd, "a.txt"));
    await symlink(join(cwd, "a.txt"), join(cwd, "b.txt"));
    const out = await runTool(
      "write_file",
      { path: "a.txt", content: "x\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/^error: /);
    expect(out).not.toMatch(/wrote \d+ bytes/);
  });
});

describe("lexical escapes stay refused", () => {
  it("refuses a relative .. escape", async () => {
    const out = await runTool("read_file", { path: "../escape.txt" }, { cwd, confirm: allow });
    expect(out).toMatch(ESCAPES);
  });

  it("refuses an absolute path outside the workspace", async () => {
    const out = await runTool(
      "read_file",
      { path: join(outside, "secret.txt") },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(ESCAPES);
    expect(out).not.toContain("OUTSIDE-SECRET");
  });

  it("refuses a sibling directory that shares the workspace name prefix", async () => {
    // /work vs /work2: a naive prefix compare would let this through.
    const sibling = `${cwd}2`;
    await mkdir(sibling);
    try {
      await writeFile(join(sibling, "secret.txt"), SECRET, "utf8");
      const out = await runTool(
        "read_file",
        { path: join(sibling, "secret.txt") },
        { cwd, confirm: allow },
      );
      expect(out).toMatch(ESCAPES);
      expect(out).not.toContain("OUTSIDE-SECRET");
    } finally {
      await rm(sibling, { recursive: true, force: true });
    }
  });
});
