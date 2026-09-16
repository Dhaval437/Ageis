/**
 * The `Copy report` bundle from the Engine-unavailable screen (`RECOVERY.md
 * § 4`, P0-17).
 *
 * The user is about to paste this into an email or a forum post, so the whole
 * point of the file is what it leaves out: **no path from the user's home, and
 * nothing key-shaped.** Core log lines already carry filesystem paths, and from
 * P2 onward they will carry text the agent scraped off the user's screen — so
 * redaction runs over every line, including the engine's own error message,
 * before anything reaches the clipboard.
 *
 * `electron`-free: this is string work over a file, and it is worth testing
 * without an Electron process.
 */

import { open, stat } from 'node:fs/promises';
import { join } from 'node:path';

/** `RECOVERY.md § 4`: "last 200 log lines". */
export const REPORT_LOG_LINES = 200;

/** Enough for 200 JSON lines; bounds the read on a 5 MB rotating log. */
const MAX_TAIL_BYTES = 512 * 1024;

/** A log line longer than this is truncated: no report needs a 10 kB line. */
const MAX_LINE_LENGTH = 500;

/** Shortest username worth substituting; below it the match is noise. */
const MIN_USER_NAME_LENGTH = 3;

export interface DiagnosticVersions {
  readonly app: string;
  readonly electron: string;
  readonly chrome: string;
  readonly node: string;
  /** e.g. `win32 10.0.26100 (x64)`. */
  readonly os: string;
}

export interface DiagnosticEngine {
  readonly status: string;
  readonly attempts: number;
  /** The supervisor's last failure message, redacted like every other line. */
  readonly lastError: string | null;
}

export interface DiagnosticInput {
  readonly versions: DiagnosticVersions;
  readonly engine: DiagnosticEngine;
  /** Oldest first, already tailed. */
  readonly logLines: readonly string[];
  /** The user's home directory, so every path under it can be removed. */
  readonly homeDir: string;
  readonly generatedAt: Date;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Matches the user's home directory and everything below it, with either
 * separator and with the doubled backslashes a JSON log line contains.
 */
function homePathPattern(homeDir: string): RegExp | null {
  const segments = homeDir.split(/[\\/]+/).filter((segment) => segment.length > 0);
  if (segments.length === 0) return null;
  const head = segments.map(escapeRegExp).join('[\\\\/]+');
  // Stop at the delimiters a path is quoted or listed with, so only the path
  // itself is replaced and the rest of the line survives.
  return new RegExp(`${head}(?:[\\\\/]+[^\\s"',;)\\]}]*)*`, 'gi');
}

/**
 * An `Authorization` value, which is two words (`Bearer <token>`) and so has to
 * be eaten to the end of the value rather than to the next space.
 */
const AUTH_VALUE = /("?\b(?:authorization|auth)\b"?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\n,;}]+)/gi;

/**
 * Key-shaped things, in the spellings the core and MAIN actually log: a JSON
 * field and a `key=value` pair.
 */
const SECRET_FIELD =
  /("?\b(?:token|api[_-]?key|secret|password|passwd)\b"?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;}]+)/gi;

/** 32+ characters of nothing but base64/hex alphabet: a token, whatever it is called. */
const OPAQUE_BLOB = /\b[A-Za-z0-9+/_-]{32,}={0,2}\b/g;

const REDACTED = '[redacted]';

/**
 * Removes secrets and the user's home paths from one line.
 *
 * Order matters: the field rule runs first so a quoted token is replaced whole,
 * and the blob rule then catches anything logged without a label.
 */
export function redactReportLine(line: string, homeDir: string): string {
  let out = line.replace(AUTH_VALUE, (_match, label: string) => `${label}${REDACTED}`);
  out = out.replace(SECRET_FIELD, (_match, label: string) => `${label}${REDACTED}`);
  out = out.replace(OPAQUE_BLOB, REDACTED);

  const home = homePathPattern(homeDir);
  if (home !== null) out = out.replace(home, '%USERPROFILE%\\…');

  const userName = homeDir
    .split(/[\\/]+/)
    .filter((segment) => segment.length > 0)
    .at(-1);
  if (userName !== undefined && userName.length >= MIN_USER_NAME_LENGTH) {
    out = out.replace(new RegExp(`\\b${escapeRegExp(userName)}\\b`, 'gi'), '%USERNAME%');
  }

  return out.length > MAX_LINE_LENGTH ? `${out.slice(0, MAX_LINE_LENGTH)}… (truncated)` : out;
}

/** `%LOCALAPPDATA%\Aegis\logs\core.log`, matching `logging_setup.LOG_FILE_NAME`. */
export function coreLogFile(logDir: string): string {
  return join(logDir, 'core.log');
}

/**
 * The last `maxLines` lines of a log file, or `[]` if there is no readable one.
 *
 * Reads at most the tail of the file rather than all of it, because the log
 * rotates at 5 MB and this runs while the user is waiting. A partial first line
 * from landing mid-line is dropped.
 */
export async function readLogTail(file: string, maxLines: number): Promise<readonly string[]> {
  let handle;
  try {
    handle = await open(file, 'r');
  } catch {
    // No log file yet — a core that never started never wrote one. Not an error.
    return [];
  }
  try {
    const { size } = await stat(file);
    const start = size > MAX_TAIL_BYTES ? size - MAX_TAIL_BYTES : 0;
    const buffer = Buffer.alloc(Math.min(size, MAX_TAIL_BYTES));
    if (buffer.length === 0) return [];
    await handle.read(buffer, 0, buffer.length, start);
    const lines = buffer
      .toString('utf8')
      .split(/\r?\n/)
      .filter((line) => line.trim().length > 0);
    if (start > 0) lines.shift();
    return lines.slice(-maxLines);
  } finally {
    await handle.close();
  }
}

/** The pasteable report. Every line that came from disk is redacted. */
export function buildDiagnosticReport(input: DiagnosticInput): string {
  const { versions, engine, homeDir } = input;
  const redact = (line: string): string => redactReportLine(line, homeDir);

  const header = [
    'Aegis diagnostic report',
    `Generated: ${input.generatedAt.toISOString()}`,
    `App: ${versions.app}`,
    `Electron: ${versions.electron} · Chromium: ${versions.chrome} · Node: ${versions.node}`,
    `OS: ${versions.os}`,
    '',
    `Engine: ${engine.status} after ${String(engine.attempts)} attempts`,
    `Last error: ${engine.lastError === null ? 'none recorded' : redact(engine.lastError)}`,
    '',
  ];

  const body =
    input.logLines.length === 0
      ? ['No engine log file was found.']
      : [
          `Last ${String(input.logLines.length)} engine log lines (paths and keys removed):`,
          ...input.logLines.map(redact),
        ];

  return [...header, ...body].join('\n');
}
