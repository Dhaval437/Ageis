/**
 * The stories every component owes (`REVIEW.md § 3`, `UI.md § 12`), enforced by
 * `tests/stories.test.tsx`.
 *
 * Each is a named export of the component's `*.stories.tsx`. A state the
 * component genuinely does not have (a button has no empty state) is declared in
 * `parameters.aegis.notApplicable` with the reason, instead of a story that
 * fakes it.
 */
export const REQUIRED_STATES = ['Default', 'Loading', 'Empty', 'Error', 'LongestContent'] as const;

export type RequiredState = (typeof REQUIRED_STATES)[number];

export interface AegisStoryParameters {
  readonly notApplicable?: Partial<Record<RequiredState, string>>;
}

/**
 * `UI.md § 12`: "A component without an error story is not done." For these,
 * `Error` can never be declared not applicable.
 */
export const INVENTORY = [
  'StepCard',
  'RiskChip',
  'ApprovalDialog',
  'OverlayHUD',
  'Composer',
  'ScopePicker',
  'AutonomyPicker',
  'LiveView',
  'TimelineList',
  'ModelRoleCard',
  'ProviderCard',
  'RuleRow',
  'SpendMeter',
  'StatusDot',
  'KillSwitchBar',
  'EmptyState',
  'ErrorCard',
  'LogTable',
  'WizardShell',
] as const;
