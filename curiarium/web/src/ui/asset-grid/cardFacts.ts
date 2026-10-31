import { type Asset } from "../../lib/api";
import { formatBytes } from "../../lib/format";

/**
 * The one compact fact line a card shows under its name.
 *
 * `Asset` has no `created_at`/`added_at` column and no bbox/volume/dimensions
 * field (geometry exists only transiently inside Three.js), so an added-date
 * or dimensions segment would have to be invented. It is deliberately absent.
 * The only facts here are the ones the row actually carries: its size and,
 * when present, how many supporting files ride along with it.
 */
export function cardFacts(item: Asset): string[] {
  const facts = [formatBytes(item.size)];
  if (item.supporting_file_count > 0) {
    facts.push(
      item.supporting_file_count === 1
        ? "1 supporting file"
        : `${item.supporting_file_count} supporting files`,
    );
  }
  return facts;
}
