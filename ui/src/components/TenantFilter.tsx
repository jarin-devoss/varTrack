/**
 * Narrow a list to one tenant. Shown only when the viewer's rules span more
 * than one: the options are the tenants they can see, never typed.
 */
export function TenantFilter({ tenants, value, onChange }: { tenants: string[]; value: string; onChange: (tenant: string) => void }) {
  if (tenants.length < 2) return null;
  return (
    <select className="ffield" aria-label="Tenant" value={value} onChange={e => onChange(e.target.value)}>
      <option value="">All tenants</option>
      {tenants.map(t => <option key={t}>{t}</option>)}
    </select>
  );
}
