import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactElement } from 'react';
import {
  ChevronDown,
  Info,
  OctagonAlert,
  RotateCcw,
  ShieldAlert,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import type { ApprovalChoice, RiskTier, RuleKind } from '@aegis/shared';
import { Button } from '@/components/ui/button';
import {
  answerFailure,
  pendingApprovals,
  ruleLabel,
  secondsLeft,
  sendAnswer,
  unreadableApprovals,
  type PendingApproval,
} from '@/lib/approvals';
import { cn } from '@/lib/utils';
import { useApprovalStore } from '@/stores/approval';
import { useStreamStore } from '@/stores/stream';

/**
 * The approval dialog (`UI.md § 5`, `P3-12`) — the highest-stakes screen in the
 * product. It asks the person about one `confirm` at a time, oldest first, and
 * answers with `POST /v1/approvals/{id}`. It lives in its own always-on-top window
 * (P3-19, `main/approval-window.ts`) and answers through that window's bridge.
 *
 * The rules it keeps, each for a reason:
 *
 * - **It is a function of the stream.** It appears on `approval.requested` and goes
 *   on `approval.resolved`, which the core sends however the question ends — so a
 *   question the core has already denied is never on screen.
 * - **Deny is the default and is always live.** It takes focus when the dialog
 *   appears, `Esc` presses it, and it is never disabled: denying is always safe.
 * - **The Allow buttons wait 200 ms** after each new approval appears, so a click
 *   or a key press the person was already making cannot approve it.
 * - **The countdown is only a display.** The core's own timer is the one that
 *   denies; this one is read from the event's timestamp so the two agree.
 * - **Nothing animates.** A moving dialog is a dialog people misclick.
 * - **The reversibility banner never lies.** It says "recoverable" only when the
 *   core says the tool has a real undo; otherwise it says it cannot be undone.
 * - **A question it cannot read is never a blank window.** It says so and offers
 *   *Deny*, and nothing else.
 */

/** `UI.md § 5`: how long the Allow buttons stay disabled after a new approval. */
export const INPUT_GUARD_MS = 200;

const TIER: Record<RiskTier, { label: string; icon: LucideIcon; tone: string }> = {
  SAFE: { label: 'Safe', icon: Info, tone: 'text-safe border-safe' },
  CAUTION: { label: 'Caution', icon: TriangleAlert, tone: 'text-caution border-caution' },
  DANGEROUS: { label: 'Dangerous', icon: OctagonAlert, tone: 'text-danger border-danger' },
  FORBIDDEN: { label: 'Forbidden', icon: ShieldAlert, tone: 'text-forbidden border-forbidden' },
};

/** Prompts longer than this are clipped with a *Show all*. */
const CLIP_CHARS = 280;

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 1000);
    return () => {
      clearInterval(timer);
    };
  }, [active]);
  return now;
}

function TierChip({ tier }: { tier: RiskTier }): ReactElement {
  const { label, icon: Icon, tone } = TIER[tier];
  // Label, icon and colour: risk is never colour alone (`UI.md § 10`).
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-pill border px-2 py-0.5 text-xs font-medium uppercase',
        tone,
      )}
    >
      <Icon aria-hidden className="size-3" />
      {label}
    </span>
  );
}

export function ApprovalDialog(): ReactElement | null {
  const events = useStreamStore((state) => state.events);
  const approval = useMemo(() => pendingApprovals(events)[0] ?? null, [events]);
  const unreadable = useMemo(() => unreadableApprovals(events)[0] ?? null, [events]);
  if (approval === null) {
    return unreadable === null ? null : <Unreadable key={unreadable} id={unreadable} />;
  }
  // Keyed by id: a new question is a new dialog, with fresh focus, menu and guard.
  return <Dialog key={approval.id} approval={approval} />;
}

/**
 * An open question whose event did not parse. Nothing of it is shown — it may be
 * anything — and it cannot be allowed; the person can deny it now or let the core's
 * timer do it.
 */
function Unreadable({ id }: { id: number }): ReactElement {
  const sending = useApprovalStore((state) => state.sending?.id === id);
  const notice = useApprovalStore((state) => (state.notice?.id === id ? state.notice.text : null));
  const { begin, settle } = useApprovalStore.getState();
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    dialogRef.current?.querySelector<HTMLElement>('[data-approval-deny]')?.focus();
  }, []);

  async function deny(): Promise<void> {
    if (sending) return;
    begin(id, 'deny');
    settle(id, answerFailure(await sendAnswer(id, 'deny'))?.text ?? null);
  }

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-surface-1">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        aria-describedby="approval-unreadable"
        onKeyDown={(event) => {
          if (event.key !== 'Escape') return;
          event.preventDefault();
          void deny();
        }}
        className="flex min-h-full w-full flex-col justify-center p-5 text-text"
      >
        <h2
          id="approval-title"
          className="flex items-center gap-2 text-md font-medium text-caution"
        >
          <TriangleAlert aria-hidden className="size-5 shrink-0" />
          Aegis needs your approval
        </h2>
        <p id="approval-unreadable" className="mt-3 text-sm text-text">
          Aegis asked for your approval, but this window could not read the question, so you cannot
          allow it. Aegis denies it when its timer runs out, or now if you press Deny.
        </p>
        <div className="mt-5 flex justify-end">
          <Button
            data-approval-deny
            variant="secondary"
            className="transition-none"
            onClick={() => {
              void deny();
            }}
          >
            {sending ? 'Denying…' : 'Deny'}
          </Button>
        </div>
        <p aria-live="assertive" className="mt-4 min-h-5 text-sm text-danger">
          {notice}
        </p>
      </div>
    </div>
  );
}

function Dialog({ approval }: { approval: PendingApproval }): ReactElement {
  const armed = useApprovalStore((state) => state.armed === approval.id);
  const sending = useApprovalStore((state) =>
    state.sending?.id === approval.id ? state.sending.choice : null,
  );
  const notice = useApprovalStore((state) =>
    state.notice?.id === approval.id ? state.notice.text : null,
  );
  const { arm, begin, settle } = useApprovalStore.getState();
  const [menuOpen, setMenuOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const now = useNow(true);

  // The guard: Allow stays disabled for 200 ms after this approval appears.
  useEffect(() => {
    const timer = setTimeout(() => {
      arm(approval.id);
    }, INPUT_GUARD_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [approval.id, arm]);

  // Deny takes focus; whatever had it gets it back when the dialog goes.
  useEffect(() => {
    const previous = document.activeElement;
    // Found by attribute: `Button` is a plain function component, so a React 18 ref
    // on it would be dropped and the focus would silently never move.
    dialogRef.current?.querySelector<HTMLElement>('[data-approval-deny]')?.focus();
    return () => {
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, []);

  async function answer(choice: ApprovalChoice, rule: RuleKind | null = null): Promise<void> {
    if (sending !== null) return;
    begin(approval.id, choice);
    settle(approval.id, answerFailure(await sendAnswer(approval.id, choice, rule))?.text ?? null);
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      void answer('deny');
      return;
    }
    if (event.key !== 'Tab') return;
    // A focus trap: Tab and Shift+Tab cycle through the dialog's live controls.
    const controls = [
      ...(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled)') ?? []),
    ];
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  const allowDisabled = !armed || sending !== null;
  const long = approval.prompt.length > CLIP_CHARS;
  const shown = long && !expanded ? `${approval.prompt.slice(0, CLIP_CHARS)}…` : approval.prompt;
  const left = secondsLeft(approval, now);
  const header = approval.tier === 'DANGEROUS' ? 'text-danger' : 'text-caution';
  const HeaderIcon = TIER[approval.tier].icon;

  return (
    // It fills its own window (P3-19), which MAIN sizes to `UI.md § 3`'s 480 px and
    // shows only while a question is pending. The window fits the longest question;
    // a shorter one sits in the middle of it, and anything longer scrolls.
    <div className="fixed inset-0 z-50 overflow-y-auto bg-surface-1">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        aria-describedby="approval-prompt"
        onKeyDown={onKeyDown}
        className="flex min-h-full w-full flex-col justify-center p-5 text-text"
      >
        <div className="flex items-start justify-between gap-3">
          <h2
            id="approval-title"
            className={cn('flex items-center gap-2 text-md font-medium', header)}
          >
            <HeaderIcon aria-hidden className="size-5 shrink-0" />
            Aegis needs your approval
          </h2>
          <TierChip tier={approval.tier} />
        </div>

        <p className="mt-1 text-sm text-text-dim">{approval.reason}</p>

        <div
          id="approval-prompt"
          className="mt-4 max-h-48 overflow-y-auto rounded-control border border-border bg-surface-2 p-3 font-mono text-sm break-words whitespace-pre-wrap"
        >
          {shown}
        </div>
        {long && (
          <Button
            variant="ghost"
            size="sm"
            className="mt-1 self-start transition-none"
            onClick={() => {
              setExpanded((value) => !value);
            }}
          >
            {expanded ? 'Show less' : 'Show all'}
          </Button>
        )}

        {approval.why !== null && (
          <p className="mt-3 text-sm text-text-dim">
            Why: <q className="text-text">{approval.why}</q>
          </p>
        )}

        <div
          className={cn(
            'mt-4 flex items-start gap-2 rounded-control border p-3 text-sm',
            approval.reversible ? 'border-border text-text' : 'border-danger text-danger',
          )}
        >
          {approval.reversible ? (
            <>
              <RotateCcw aria-hidden className="mt-0.5 size-4 shrink-0" />
              Recoverable: Aegis can undo this afterwards.
            </>
          ) : (
            <>
              <OctagonAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
              This cannot be undone.
            </>
          )}
        </div>

        <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
          <Button
            data-approval-deny
            variant="secondary"
            className="transition-none"
            onClick={() => {
              void answer('deny');
            }}
          >
            {sending === 'deny' ? 'Denying…' : 'Deny'}
          </Button>
          <Button
            variant={approval.tier === 'DANGEROUS' ? 'danger' : 'accent'}
            className="transition-none"
            disabled={allowDisabled}
            onClick={() => {
              void answer('allow');
            }}
          >
            {sending === 'allow' ? 'Allowing…' : 'Allow once'}
          </Button>
          {approval.allowAlways && (
            <Button
              variant="secondary"
              className="transition-none"
              disabled={allowDisabled}
              aria-expanded={menuOpen}
              aria-controls="approval-rules"
              onClick={() => {
                setMenuOpen((open) => !open);
              }}
            >
              {sending === 'allow_always' ? 'Saving…' : 'Allow always'}
              <ChevronDown aria-hidden />
            </Button>
          )}
        </div>

        {approval.allowAlways && menuOpen && (
          <div
            id="approval-rules"
            role="group"
            aria-label="Allow always for"
            className="mt-2 flex flex-col gap-1 rounded-control border border-border bg-surface-2 p-1"
          >
            {approval.ruleKinds.map((kind) => (
              <Button
                key={kind}
                variant="ghost"
                className="justify-start truncate transition-none"
                disabled={allowDisabled}
                onClick={() => {
                  void answer('allow_always', kind);
                }}
              >
                {ruleLabel(kind, approval)}
              </Button>
            ))}
          </div>
        )}

        <p className="mt-4 text-sm text-text-dim" aria-hidden>
          Deny in {left}s
        </p>
        <p aria-live="assertive" className="mt-1 min-h-5 text-sm text-danger">
          {notice}
        </p>
      </div>
    </div>
  );
}
