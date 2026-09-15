import { composeStories, setProjectAnnotations, type composeStory } from '@storybook/react-vite';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import preview from '../.storybook/preview';
import { INVENTORY, REQUIRED_STATES, type AegisStoryParameters } from '../.storybook/states';

/**
 * `REVIEW.md § 3` as a test: every component has stories, every story file owes
 * default / loading / empty / error / longest-content or says why a state does
 * not exist, and every story renders. A Storybook nobody opens still fails here.
 */

setProjectAnnotations(preview);

type StoriesModule = Record<string, unknown> & {
  default: { title?: string; parameters?: { aegis?: AegisStoryParameters } };
};

type ComposedStory = ReturnType<typeof composeStory>;

const COMPONENT_FILES = Object.keys(import.meta.glob('../src/components/**/*.tsx')).filter(
  (path) => !path.endsWith('.stories.tsx'),
);
const STORY_MODULES = import.meta.glob<StoriesModule>('../src/components/**/*.stories.tsx', {
  eager: true,
});

/** By export name, so a required state can be looked up rather than typed in. */
function storiesOf(module: StoriesModule): Map<string, ComposedStory> {
  return new Map(Object.entries(composeStories(module)));
}

async function renderStory(Story: ComposedStory): Promise<ReturnType<typeof render>> {
  // Runs the preview's and the story's `beforeEach`, which is where state is set.
  await Story.load();
  return render(<Story />);
}

describe('Storybook coverage', () => {
  it('finds the components and their stories', () => {
    expect(COMPONENT_FILES.length).toBeGreaterThan(0);
    expect(Object.keys(STORY_MODULES).length).toBeGreaterThan(0);
  });

  it.each(COMPONENT_FILES)('%s has a stories file beside it', (path) => {
    expect(Object.keys(STORY_MODULES)).toContain(path.replace(/\.tsx$/, '.stories.tsx'));
  });

  describe.each(Object.entries(STORY_MODULES))('%s', (path, module) => {
    const notApplicable = module.default.parameters?.aegis?.notApplicable ?? {};
    const stories = storiesOf(module);

    it.each(REQUIRED_STATES)('covers %s, or says why it cannot', (state) => {
      const reason = notApplicable[state];
      if (reason === undefined) {
        expect(stories.has(state), `export a ${state} story, or declare why it cannot exist`).toBe(
          true,
        );
      } else {
        expect(stories.has(state), `${state} is exported and declared not applicable`).toBe(false);
        expect(reason.trim(), `${state} needs a reason`).not.toBe('');
      }
    });

    it('declares only states that exist', () => {
      for (const state of Object.keys(notApplicable)) {
        expect(REQUIRED_STATES).toContain(state);
      }
    });

    it('never skips the error story for a UI.md § 12 component', () => {
      const name = path.replace(/^.*\//, '').replace(/\.stories\.tsx$/, '');
      if ((INVENTORY as readonly string[]).includes(name)) {
        expect(notApplicable.Error).toBeUndefined();
      }
    });

    it.each([...stories])('renders %s', async (_name, Story) => {
      const { container } = await renderStory(Story);
      expect(container).not.toBeEmptyDOMElement();
    });
  });
});

describe('story state', () => {
  const shell = STORY_MODULES['../src/components/AppShell.stories.tsx'];
  const stories = shell ? storiesOf(shell) : new Map<string, ComposedStory>();

  // Without this, a `beforeEach` that never ran would still render *something*.
  it.each([
    ['Default', null],
    ['Loading', 'Starting the engine…'],
    ['Error', 'The engine is not running.'],
    ['Reconnecting', 'Reconnecting…'],
  ] as const)('AppShell %s shows its engine state', async (name, note) => {
    const Story = stories.get(name);
    if (!Story) throw new Error(`AppShell has no ${name} story`);
    const { queryByRole } = await renderStory(Story);
    if (note === null) {
      expect(queryByRole('status')).not.toBeInTheDocument();
    } else {
      expect(queryByRole('status')).toHaveTextContent(note);
    }
  });
});
