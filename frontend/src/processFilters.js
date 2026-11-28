export const EMPTY_FILTERS = Object.freeze({ search: '', tribunal: '', outcome: '', date_from: '', date_to: '', value_min: '', value_max: '', instancia: '' });
export const TRIBUNALS = ['TST', ...Array.from({length:24}, (_,i)=>`TRT${i+1}`)];
export function validateFilters(f) {
  if (f.date_from && f.date_to && f.date_from > f.date_to) return 'A data inicial deve ser anterior ou igual à final.';
  for (const key of ['value_min','value_max']) if (f[key] !== '' && (!Number.isFinite(Number(f[key])) || Number(f[key]) < 0)) return 'Informe valores não negativos.';
  if (f.value_min !== '' && f.value_max !== '' && Number(f.value_min)>Number(f.value_max)) return 'O valor mínimo deve ser menor ou igual ao máximo.';
  return '';
}
export function processQuery(filters, page=1) {
  const params = new URLSearchParams({page:String(page),per_page:'20'});
  for (const key of Object.keys(EMPTY_FILTERS)) if (String(filters[key] ?? '').trim() !== '') params.set(key,String(filters[key]).trim());
  return params.toString();
}
// US-08 must acknowledge advanced parameters; older servers silently ignore unknown keys.
export function advancedFiltersConfirmed(filters, response) {
  return ['date_from','date_to','value_min','value_max'].every(key => !String(filters[key] ?? '').trim() || (response?.applied_filters?.[key] != null && String(response.applied_filters[key]) !== '' && (key.startsWith('value_') ? Number(response.applied_filters[key]) === Number(filters[key]) : String(response.applied_filters[key]) === String(filters[key]))));
}
