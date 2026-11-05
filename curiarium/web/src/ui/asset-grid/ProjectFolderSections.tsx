import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import type { Asset } from "../../lib/api";
import { GROUP_CARD_CAP } from "./assetGridMeta";
import type { ProjectGroup } from "./projectSections";

type Props = {
  groups: ProjectGroup[];
  renderCard: (item: Asset) => ReactNode;
  /**
   * The listing has more pages, so the counts below describe the loaded page,
   * not the whole project. Defaults to false (a fully loaded project).
   */
  hasMore?: boolean;
};

/** Chip id standing for "no filter"; group ids are never this value. */
const ALL_CHIP_ID = "all";

/**
 * The project's sectioned view: a top-level folder filter row over the parts,
 * grouped into folder sections. The filter is a predicate over the already
 * loaded items - it issues no request and cannot refetch, so switching waves is
 * instant and the counts stay those of the listing that was loaded.
 */
export default function ProjectFolderSections({ groups, renderCard, hasMore = false }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set());
  const chipRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const total = groups.reduce((sum, group) => sum + group.count, 0);
  // Resolved on every render rather than reset in an effect: a stored id that
  // no longer exists (a project switch) falls back to All by itself, so no
  // effect can strand a stale filter over a different project's parts.
  const active = groups.find(group => group.id === selected) ?? null;
  const visibleGroups = active ? [active] : groups;
  const visibleParts = visibleGroups.reduce((sum, group) => sum + group.count, 0);

  const chipIds = [ALL_CHIP_ID, ...groups.map(group => group.id)];

  const selectAndFocus = (chipId: string) => {
    setSelected(chipId === ALL_CHIP_ID ? null : chipId);
    chipRefs.current[chipId]?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = chipIds.indexOf(selected ?? ALL_CHIP_ID);
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        event.preventDefault();
        selectAndFocus(chipIds[(index + 1) % chipIds.length]);
        return;
      case "ArrowLeft":
      case "ArrowUp":
        event.preventDefault();
        selectAndFocus(chipIds[(index - 1 + chipIds.length) % chipIds.length]);
        return;
      case "Home":
        event.preventDefault();
        selectAndFocus(chipIds[0]);
        return;
      case "End":
        event.preventDefault();
        selectAndFocus(chipIds[chipIds.length - 1]);
        return;
      default:
        return;
    }
  };

  const toggleSection = (key: string) => {
    setExpandedSections(previous => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const chip = (chipId: string, label: string, checked: boolean) => (
    <button
      key={chipId}
      ref={element => {
        chipRefs.current[chipId] = element;
      }}
      type="button"
      role="radio"
      aria-checked={checked}
      tabIndex={checked ? 0 : -1}
      data-project-group={`chip:${chipId}`}
      className="px-3 py-1.5 rounded-md border border-panel-strong text-sm"
      onClick={() => selectAndFocus(chipId)}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-4" data-project-sections>
      <div
        role="radiogroup"
        aria-label="Filter by folder"
        className="flex flex-wrap gap-2"
        onKeyDown={handleKeyDown}
      >
        {/* Checked follows the RESOLVED selection, so a stale id shows All. */}
        {chip(ALL_CHIP_ID, `All ${total}`, active === null)}
        {groups.map(group => chip(group.id, `${group.title} ${group.count}`, group.id === active?.id))}
      </div>
      <p className="text-sm opacity-70" aria-live="polite" data-project-visible-count>
        {visibleCountLabel(visibleParts, total, active?.title ?? null, hasMore)}
      </p>
      {visibleGroups.flatMap(group =>
        group.sections.map(section => {
          const expanded = expandedSections.has(section.key);
          const capped = section.items.length > GROUP_CARD_CAP;
          const visibleItems = expanded ? section.items : section.items.slice(0, GROUP_CARD_CAP);
          const headerLabel = sectionHeaderLabel(group.title, section.title);
          return (
            <section
              key={section.key}
              data-project-section={section.key}
              aria-label={headerLabel}
              className="rounded-lg border border-panel bg-panel-soft p-4 space-y-3"
            >
              <h3 className="font-medium">
                {headerLabel}
                <span className="ml-2 text-xs opacity-70" data-section-count>
                  {section.items.length}
                </span>
              </h3>
              <div
                className="grid gap-4"
                data-section-grid
                style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}
              >
                {visibleItems.map(item => (
                  <div key={item.id}>{renderCard(item)}</div>
                ))}
              </div>
              {capped && (
                <button
                  type="button"
                  className="px-3 py-2 rounded-md border border-panel-strong text-sm"
                  data-section-escape={section.key}
                  onClick={() => toggleSection(section.key)}
                >
                  {expanded ? "Show fewer" : `Show all ${section.items.length}`}
                </button>
              )}
            </section>
          );
        }),
      )}
    </div>
  );
}

/**
 * The header text: the group is the top-level folder, so a section only adds
 * its own level below it. One level below the trimmed root leaves the section
 * title empty and the group title alone is the header.
 */
function sectionHeaderLabel(groupTitle: string, sectionTitle: string): string {
  return sectionTitle ? `${groupTitle} . ${sectionTitle}` : groupTitle;
}

/** Counts are the loaded items behind the current filter, never the rendered cards. */
function visibleCountLabel(
  visible: number,
  total: number,
  activeTitle: string | null,
  hasMore: boolean,
): string {
  const scope = activeTitle ? ` in ${activeTitle}` : "";
  // A paged project's total is the loaded total, so say so rather than letting
  // "Showing 1000 of 1000 parts" read as the whole project.
  if (hasMore) return `Showing ${visible} of ${total} loaded parts${scope} - more to load`;
  return `Showing ${visible} of ${total} parts${scope}`;
}
