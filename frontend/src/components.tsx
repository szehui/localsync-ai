import React from 'react';

// ─── Shared UI Components ────────────────────────────────────────────────────

const cx = (...classes: (string | boolean | undefined | null)[]) =>
  classes.filter(Boolean).join(' ');

export function Card({ children, className = '', hover = false }: { children: React.ReactNode; className?: string; hover?: boolean }) {
  return (
    <div
      className={cx(
        'rounded-xl border border-surface-border bg-surface-raised p-5 transition-all duration-200',
        hover && 'hover:border-surface-highlight hover:shadow-lg hover:shadow-black/10',
        className,
      )}
    >
      {children}
    </div>
  );
}

export function Button({
  children,
  onClick,
  variant = 'primary',
  disabled = false,
  className = '',
  type = 'button',
  size = 'md',
  loading = false,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'accent';
  disabled?: boolean;
  className?: string;
  type?: 'button' | 'submit';
  size?: 'sm' | 'md';
  loading?: boolean;
}) {
  const sizes = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
  };
  const variants = {
    primary: 'bg-accent hover:bg-accent-soft text-black font-semibold shadow-sm shadow-accent/20',
    secondary: 'bg-surface-overlay hover:bg-surface-highlight text-gray-200 border border-surface-border',
    danger: 'bg-red-600 hover:bg-red-700 text-white',
    ghost: 'bg-transparent hover:bg-surface-highlight text-gray-400 hover:text-gray-200',
    accent: 'bg-accent/10 hover:bg-accent/20 text-accent border border-accent/20',
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      className={cx(
        'rounded-lg font-medium transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-2',
        sizes[size],
        variants[variant],
        className,
      )}
    >
      {loading && <SpinnerIcon />}
      {children}
    </button>
  );
}

function SpinnerIcon() {
  return (
    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

export function Badge({
  children,
  variant = 'default',
  dot = false,
  className = '',
}: {
  children?: React.ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info';
  dot?: boolean;
  className?: string;
}) {
  const variants = {
    default: 'bg-surface-overlay text-gray-400 border border-surface-border',
    success: 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20',
    warning: 'bg-amber-500/10 text-amber-400 border border-amber-500/20',
    danger: 'bg-red-500/10 text-red-400 border border-red-500/20',
    info: 'bg-accent/10 text-accent border border-accent/20',
  };
  return (
    <span className={cx('inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium', variants[variant], className)}>
      {dot && <span className={cx(
        'w-1.5 h-1.5 rounded-full',
        variant === 'success' && 'bg-emerald-400',
        variant === 'warning' && 'bg-amber-400',
        variant === 'danger' && 'bg-red-400',
        variant === 'info' && 'bg-accent',
        variant === 'default' && 'bg-gray-400',
      )} />}
      {children}
    </span>
  );
}

export function Input({
  value,
  onChange,
  placeholder,
  type = 'text',
  className = '',
  label,
  error,
  onKeyPress,
}: {
  value: string;
  onChange: (val: string) => void;
  placeholder?: string;
  type?: string;
  className?: string;
  label?: string;
  error?: string;
  onKeyPress?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
}) {
  return (
    <div>
      {label && <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">{label}</label>}
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyPress={onKeyPress}
        placeholder={placeholder}
        className={cx(
          'w-full rounded-lg border bg-surface-overlay px-3.5 py-2.5 text-sm text-white placeholder-gray-500',
          'transition-all duration-150',
          'focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent',
          error ? 'border-red-500/50' : 'border-surface-border',
          className,
        )}
      />
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  );
}

export function Slider({
  value,
  onChange,
  min = 1,
  max = 5,
  step = 1,
  label,
  labels,
}: {
  value: number;
  onChange: (val: number) => void;
  min?: number;
  max?: number;
  step?: number;
  label: string;
  labels?: string[];
}) {
  const pct = ((value - min) / (max - min)) * 100;
  return (
    <div>
      <div className="flex justify-between items-center mb-2">
        <label className="text-sm font-medium text-gray-300">{label}</label>
        <span className="text-sm text-accent font-medium">
          {labels ? labels[value - min] : value}
        </span>
      </div>
      <div className="relative">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-full h-1.5 appearance-none cursor-pointer rounded-full bg-surface-highlight accent-accent
            [&::-webkit-slider-thumb]:appearance-none
            [&::-webkit-slider-thumb]:w-4
            [&::-webkit-slider-thumb]:h-4
            [&::-webkit-slider-thumb]:rounded-full
            [&::-webkit-slider-thumb]:bg-accent
            [&::-webkit-slider-thumb]:shadow-lg
            [&::-webkit-slider-thumb]:shadow-accent/30
            [&::-webkit-slider-thumb]:transition-transform
            [&::-webkit-slider-thumb]:duration-150
            [&::-webkit-slider-thumb]:hover:scale-110
            [&::-moz-range-thumb]:w-4
            [&::-moz-range-thumb]:h-4
            [&::-moz-range-thumb]:rounded-full
            [&::-moz-range-thumb]:bg-accent
            [&::-moz-range-thumb]:border-0"
          style={{
            background: `linear-gradient(to right, #2dd4bf 0%, #2dd4bf ${pct}%, #323236 ${pct}%, #323236 100%)`,
          }}
        />
      </div>
      {labels && (
        <div className="flex justify-between mt-1.5">
          <span className="text-xs text-gray-500">{labels[0]}</span>
          <span className="text-xs text-gray-500">{labels[labels.length - 1]}</span>
        </div>
      )}
    </div>
  );
}

export function Select({
  value,
  onChange,
  options,
  placeholder,
  label,
}: {
  value: string;
  onChange: (val: string) => void;
  options: { value: string; label: string }[];
  placeholder?: string;
  label?: string;
}) {
  return (
    <div>
      {label && <label className="block text-xs font-medium text-gray-400 mb-1.5 tracking-wide uppercase">{label}</label>}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-surface-border bg-surface-overlay px-3.5 py-2.5 text-sm text-white
          focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-all duration-150"
      >
        {placeholder && <option value="" className="text-gray-500">{placeholder}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

export function Spinner({ text = 'Loading...' }: { text?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-12 text-gray-400">
      <svg className="animate-spin h-5 w-5 text-accent" viewBox="0 0 24 24" fill="none">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
      <span className="text-sm">{text}</span>
    </div>
  );
}

export function EmptyState({ message, icon = '📭' }: { message: string; icon?: string }) {
  return (
    <div className="text-center py-16 animate-fade-in">
      <div className="text-3xl mb-3 opacity-50">{icon}</div>
      <p className="text-gray-500 text-sm max-w-sm mx-auto">{message}</p>
    </div>
  );
}

export function formatDuration(seconds?: number): string {
  if (!seconds) return '--:--';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function formatDate(dateStr?: string): string {
  if (!dateStr) return 'Never';
  try {
    return new Date(dateStr).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return dateStr;
  }
}

export function formatRelativeTime(dateStr?: string): string {
  if (!dateStr) return '';
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diff = now - then;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function SectionHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h2 className="text-lg font-semibold text-white">{title}</h2>
        {subtitle && <p className="text-sm text-gray-500 mt-0.5">{subtitle}</p>}
      </div>
      {action && <div>{action}</div>}
    </div>
  );
}

export function NumberStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-surface-overlay rounded-xl p-4 text-center border border-surface-border">
      <div className="text-2xl font-bold text-white tabular-nums">{value}</div>
      <div className="text-xs text-gray-500 mt-1 uppercase tracking-wide">{label}</div>
    </div>
  );
}

export function TrackRow({
  track,
  index,
  isSelected,
  onClick,
  showIndex = true,
}: {
  track: { id: string; title: string; artist_name?: string; album_name?: string; duration?: number };
  index: number;
  isSelected?: boolean;
  onClick?: () => void;
  showIndex?: boolean;
}) {
  return (
    <tr
      onClick={onClick}
      className={cx(
        'border-b border-surface-border/50 transition-colors duration-100',
        'hover:bg-surface-highlight/50',
        isSelected && 'bg-accent/5',
        onClick && 'cursor-pointer',
      )}
    >
      {showIndex && (
        <td className="px-3 py-2.5 text-xs text-gray-600 w-8 text-center tabular-nums">{index + 1}</td>
      )}
      <td className="px-3 py-2.5">
        <span className="text-sm font-medium text-white">{track.title}</span>
      </td>
      <td className="px-3 py-2.5 text-sm text-gray-400">{track.artist_name || 'Unknown'}</td>
      <td className="px-3 py-2.5 text-sm text-gray-500 hidden sm:table-cell">{track.album_name || '—'}</td>
      <td className="px-3 py-2.5 text-sm text-gray-500 text-right tabular-nums">{formatDuration(track.duration)}</td>
    </tr>
  );
}

export function Table({
  headers,
  children,
  maxHeight,
}: {
  headers: string[];
  children: React.ReactNode;
  maxHeight?: string;
}) {
  return (
    <div className={cx('rounded-lg border border-surface-border overflow-hidden', maxHeight && 'overflow-y-auto')} style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full">
        <thead>
          <tr className="bg-surface-overlay/80 border-b border-surface-border">
            {headers.map((h, i) => (
              <th key={i} className={cx(
                'text-left px-3 py-2.5 text-xs font-medium text-gray-500 uppercase tracking-wider',
                i === 0 && 'w-8 text-center',
                i === headers.length - 1 && 'text-right',
              )}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border/50">
          {children}
        </tbody>
      </table>
    </div>
  );
}

export function Divider({ className = '' }: { className?: string }) {
  return <hr className={cx('border-surface-border my-6', className)} />;
}
