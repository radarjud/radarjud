import React from 'react';
import { EMPTY_FILTERS, TRIBUNALS } from './processFilters.js';
import './processFilters.css';

export default function ProcessFiltersBar({
  filters,
  onChange,
  disabled,
  error
}) {
  const field = (key) => ({
    value: filters[key],
    onChange: (e) =>
      onChange({ ...filters, [key]: e.target.value }),
    disabled
  });

  return (
    <fieldset className="process-filters" disabled={disabled}>
      <legend>Filtros avançados</legend>

      <div className="process-filters-primary">
        <label>
          Pesquisar
          <input
            {...field('search')}
            type="search"
            placeholder="Número, assunto ou observação"
          />
        </label>

        <label>
          Tribunal
          <select {...field('tribunal')}>
            <option value="">Todos</option>
            {TRIBUNALS.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>

        <label>
          Desfecho
          <select {...field('outcome')}>
            <option value="">Todos</option>
            <option value="condenacoes">Condenações</option>
            <option value="acordos">Acordos</option>
            <option value="improcedentes">Improcedentes</option>
            <option value="em_aberto">Em aberto</option>
          </select>
        </label>
      </div>

      <div className="process-filters-secondary">
        <label>
          Data inicial
          <input
            {...field('date_from')}
            type="date"
            max={filters.date_to || undefined}
          />
        </label>

        <label>
          Data final
          <input
            {...field('date_to')}
            type="date"
            min={filters.date_from || undefined}
          />
        </label>

        <label>
          Valor mínimo (R$)
          <input
            {...field('value_min')}
            type="number"
            min="0"
            step="0.01"
            placeholder="Sem mínimo"
          />
        </label>

        <label>
          Valor máximo (R$)
          <input
            {...field('value_max')}
            type="number"
            min="0"
            step="0.01"
            placeholder="Sem máximo"
          />
        </label>
      </div>

      <div className="process-filters-actions">
        <button
          type="button"
          className="secondary"
          onClick={() => onChange({ ...EMPTY_FILTERS })}
        >
          Limpar filtros
        </button>
      </div>

      {error && (
        <p role="alert" className="process-filter-error">
          {error}
        </p>
      )}
    </fieldset>
  );
}
