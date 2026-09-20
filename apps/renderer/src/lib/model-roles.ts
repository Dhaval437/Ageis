import type { ModelSettings } from '@aegis/shared';

/**
 * The three roles the agent uses (`ARCHITECTURE.md § 5.2`), as the Models screen
 * names and explains them.
 *
 * The wording is the reason this is a module rather than a literal in the
 * component: a user picking a model has to know what that model will be asked to
 * do, and `UI.md § 11` wants it in plain second-person terms rather than in the
 * architecture's.
 */

export type RoleId = 'planner' | 'grounder' | 'utility';

export interface RoleDescription {
  readonly id: RoleId;
  readonly label: string;
  readonly description: string;
}

export const ROLES: readonly [RoleDescription, RoleDescription, RoleDescription] = [
  {
    id: 'planner',
    label: 'Planner',
    description:
      'Decides what to do next and how to recover when a step fails. Wants strong reasoning.',
  },
  {
    id: 'grounder',
    label: 'Grounder',
    description:
      'Looks at your screen and says which thing to click. Needs vision, and is called often.',
  },
  {
    id: 'utility',
    label: 'Utility',
    description: 'Summarises, extracts text, names a file. A small or local model is enough.',
  },
];

/**
 * How many models one role may name, matching `MAX_CHAIN` in
 * `models/router.py` — primary plus three. The screen stops offering a fifth
 * rather than letting the save fail on a rule the user cannot see.
 */
export const MAX_FALLBACKS = 3;

/** The route saved for one role, or `null` while it is unmapped. */
export function routeOf(settings: ModelSettings, role: RoleId): ModelSettings[RoleId] {
  return settings[role];
}

/** `settings` with one role replaced. Roles are named fields, not a map. */
export function withRoute(
  settings: ModelSettings,
  role: RoleId,
  route: ModelSettings[RoleId],
): ModelSettings {
  return { ...settings, [role]: route };
}

/** Which roles still have no model. The screen says so rather than failing later. */
export function unmappedRoles(settings: ModelSettings): readonly RoleDescription[] {
  return ROLES.filter((role) => settings[role.id] === null);
}
