import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { RuleView } from '../api/types';
import { EmptyState, ErrorState, LoadingRows } from '../components/common';
import { FilterBar } from '../components/FilterBar';
import { SearchBox } from '../components/SearchBox';
import { RuleCard } from '../features/ruleViews';
import { SyncPanel } from '../features/SyncPanel';
import { usePageTitle, useSectionCount } from '../layout/page';
import { matchesSearch } from '../lib/filters';
import { usePinned } from '../lib/prefs';
import { isPinned, ruleKey, tenantsOf } from '../lib/rules';
import { useRules } from '../lib/useRules';

export function FavoritesPage() {
  usePageTitle('Favorites');
  const rules = useRules();
  const [pinned, togglePin] = usePinned();
  const [search, setSearch] = useState('');
  const [syncing, setSyncing] = useState<RuleView | null>(null);

  const favorites = (rules.data ?? []).filter(r => isPinned(pinned, r));
  const tenants = tenantsOf(rules.data ?? []);
  const shown = favorites.filter(r => matchesSearch(r, search));
  useSectionCount('/favorites', rules.data ? favorites.length : undefined, 'favorites', rules.loading);

  if (rules.error && !rules.data) {
    return (
      <div className="card">
        <ErrorState error={rules.error} what="rules" onRetry={rules.reload} />
      </div>
    );
  }
  if (!rules.data) return <LoadingRows rows={3} height={120} />;

  if (favorites.length === 0) {
    return (
      <div className="card">
        <EmptyState icon="star" title="No favorites yet" action={<Link className="btn btn--primary btn--sm" to="/rules">Browse rules</Link>}>
          Star the rules you look after — on the Rules list or a rule's page — and they'll show up here with their current status.
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="stack">
      {favorites.length > 3 && (
        <FilterBar count={shown.length} label="favorites">
          <SearchBox value={search} onChange={setSearch} placeholder="Search favorites…" hotkey />
        </FilterBar>
      )}
      <div className="rcard-grid">
        {shown.map(r => (
          <RuleCard key={ruleKey(r)} rule={r} pinned onTogglePin={() => togglePin(r)} onSync={() => setSyncing(r)} showTenant={tenants.length > 1} />
        ))}
      </div>
      <SyncPanel open={!!syncing} rule={syncing} onClose={() => setSyncing(null)} onStarted={rules.reload} />
    </div>
  );
}
