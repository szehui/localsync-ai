import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';
import { 
  Card, Button, Badge, Input, Select, Spinner, EmptyState, 
  formatDate, SectionHeader 
} from '../components';
import type { Trigger, TriggerCreate } from '../types';

const TRIGGER_TYPES = [
  { value: 'recency', label: 'Fresh Discoveries (daily)' },
  { value: 'heavy_rotation', label: 'Heavy Rotation (every 6h)' },
  { value: 'scheduled', label: 'Scheduled (custom cron)' },
];

const TRIGGER_ICONS: Record<string, string> = {
  recency: '🆕',
  heavy_rotation: '🔄',
  scheduled: '⏰',
};

export function AutomationHub() {
  const [triggers, setTriggers] = useState<Trigger[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState('');

  // Form state
  const [formName, setFormName] = useState('');
  const [formType, setFormType] = useState('recency');
  const [formCron, setFormCron] = useState('');
  const [formThreshold, setFormThreshold] = useState('5');
  const [formPlaylistName, setFormPlaylistName] = useState('');

  const loadTriggers = useCallback(async () => {
    setLoading(true);
    try {
      const t = await api.getTriggers();
      setTriggers(t);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load triggers');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadTriggers(); }, [loadTriggers]);

  const resetForm = () => {
    setFormName('');
    setFormCron('');
    setFormThreshold('5');
    setFormPlaylistName('');
  };

  const handleCreate = async () => {
    setError('');
    const create: TriggerCreate = {
      name: formName || `${formType} trigger`,
      trigger_type: formType as TriggerCreate['trigger_type'],
      playlist_name: formPlaylistName || undefined,
    };
    if (formType === 'scheduled' && formCron) {
      create.cron_expression = formCron;
    }
    if (formType === 'heavy_rotation') {
      create.threshold = parseInt(formThreshold) || 5;
    }
    try {
      await api.createTrigger(create);
      setShowForm(false);
      resetForm();
      await loadTriggers();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create trigger');
    }
  };

  const handleToggle = async (id: number) => {
    try {
      await api.toggleTrigger(id);
      await loadTriggers();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to toggle trigger');
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await api.deleteTrigger(id);
      await loadTriggers();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to delete trigger');
    }
  };

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Automation Hub"
        subtitle="Automatically generate playlists on a schedule"
        action={
          <Button onClick={() => { setShowForm(!showForm); resetForm(); }}>
            {showForm ? 'Cancel' : '+ New Trigger'}
          </Button>
        }
      />

      {error && (
        <div className="bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {/* Create Form */}
      {showForm && (
        <Card className="animate-slide-up">
          <h3 className="font-medium text-white mb-5">Create Smart Trigger</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 mb-5">
            <Input
              value={formName}
              onChange={setFormName}
              placeholder="My Trigger"
              label="Name"
            />
            <Select
              value={formType}
              onChange={setFormType}
              options={TRIGGER_TYPES}
              label="Type"
            />
            <Input
              value={formPlaylistName}
              onChange={setFormPlaylistName}
              placeholder="Auto-generated if empty"
              label="Playlist Name (optional)"
            />
            {formType === 'scheduled' && (
              <Input
                value={formCron}
                onChange={setFormCron}
                placeholder="0 17 * * 5 (Fri 5pm)"
                label="Cron Expression"
              />
            )}
            {formType === 'heavy_rotation' && (
              <Input
                value={formThreshold}
                onChange={setFormThreshold}
                placeholder="5"
                type="number"
                label="Play Threshold"
              />
            )}
          </div>
          <div className="flex justify-end">
            <Button onClick={handleCreate}>Create Trigger</Button>
          </div>
        </Card>
      )}

      {/* Triggers List */}
      {loading && <Spinner text="Loading triggers…" />}
      {!loading && triggers.length === 0 && (
        <EmptyState
          message="No triggers configured. Create one to automate your playlists."
          icon="⚙️"
        />
      )}

      {!loading && triggers.length > 0 && (
        <div className="space-y-3">
          {triggers.map((trigger) => (
            <Card key={trigger.id} hover className="animate-slide-up">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-base">{TRIGGER_ICONS[trigger.trigger_type] || '⚡'}</span>
                    <h3 className="font-medium text-white truncate">{trigger.name}</h3>
                    <Badge
                      variant={trigger.enabled ? 'success' : 'default'}
                      dot={trigger.enabled}
                    >
                      {trigger.enabled ? 'Enabled' : 'Disabled'}
                    </Badge>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-xs text-gray-500">
                    <span className="capitalize">{trigger.trigger_type.replace('_', ' ')}</span>
                    {trigger.threshold && (
                      <>
                        <span className="text-gray-600">|</span>
                        <span>Threshold: <span className="text-gray-400">{trigger.threshold} plays</span></span>
                      </>
                    )}
                    {trigger.cron_expression && (
                      <>
                        <span className="text-gray-600">|</span>
                        <span>Cron: <code className="px-1 py-0.5 rounded bg-surface-overlay text-accent text-[10px]">{trigger.cron_expression}</code></span>
                      </>
                    )}
                  </div>
                  {trigger.playlist_name && (
                    <p className="text-xs text-gray-500 mt-1">
                      → <span className="text-gray-400">{trigger.playlist_name}</span>
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-600">
                    {trigger.last_run && <span>Last run: {formatDate(trigger.last_run)}</span>}
                    {trigger.next_run && (
                      <>
                        <span>·</span>
                        <span>Next run: {formatDate(trigger.next_run)}</span>
                      </>
                    )}
                    {trigger.navidrome_playlist_id && (
                      <>
                        <span>·</span>
                        <span className="text-emerald-500/70">✓ Linked to Navidrome</span>
                      </>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button
                    variant={trigger.enabled ? 'ghost' : 'accent'}
                    onClick={() => handleToggle(trigger.id)}
                    size="sm"
                  >
                    {trigger.enabled ? 'Disable' : 'Enable'}
                  </Button>
                  <Button
                    variant="ghost"
                    onClick={() => handleDelete(trigger.id)}
                    size="sm"
                    className="text-red-400 hover:text-red-300 hover:bg-red-500/10"
                  >
                    Delete
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
