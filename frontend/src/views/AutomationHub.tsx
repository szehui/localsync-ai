import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';
import { Card, Button, Badge, Input, Select, Spinner, EmptyState, formatDate } from '../components';
import type { Trigger, TriggerCreate } from '../types';

const TRIGGER_TYPES = [
  { value: 'recency', label: 'Fresh Discoveries (daily)' },
  { value: 'heavy_rotation', label: 'Heavy Rotation (every 6h)' },
  { value: 'scheduled', label: 'Scheduled (custom cron)' },
];

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
      setFormName('');
      setFormCron('');
      setFormThreshold('5');
      setFormPlaylistName('');
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
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Automation Hub</h2>
        <Button onClick={() => setShowForm(!showForm)}>
          {showForm ? 'Cancel' : '+ New Trigger'}
        </Button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* Create Form */}
      {showForm && (
        <Card>
          <h3 className="font-medium mb-4">Create Smart Trigger</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">Name</label>
              <Input
                value={formName}
                onChange={setFormName}
                placeholder="My Trigger"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Type</label>
              <Select
                value={formType}
                onChange={setFormType}
                options={TRIGGER_TYPES}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Playlist Name (optional)</label>
              <Input
                value={formPlaylistName}
                onChange={setFormPlaylistName}
                placeholder="Auto-generated if empty"
              />
            </div>
            {formType === 'scheduled' && (
              <div>
                <label className="block text-sm text-gray-400 mb-1">Cron Expression</label>
                <Input
                  value={formCron}
                  onChange={setFormCron}
                  placeholder="0 17 * * 5 (Fri 5pm)"
                />
              </div>
            )}
            {formType === 'heavy_rotation' && (
              <div>
                <label className="block text-sm text-gray-400 mb-1">Play Threshold</label>
                <Input
                  value={formThreshold}
                  onChange={setFormThreshold}
                  placeholder="5"
                  type="number"
                />
              </div>
            )}
          </div>
          <Button onClick={handleCreate}>Create Trigger</Button>
        </Card>
      )}

      {/* Triggers List */}
      {loading && <Spinner />}
      {!loading && triggers.length === 0 && (
        <EmptyState message="No triggers configured. Create one to automate your playlists." />
      )}

      <div className="space-y-4">
        {triggers.map((trigger) => (
          <Card key={trigger.id}>
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="font-medium text-white">{trigger.name}</h3>
                  <Badge variant={trigger.enabled ? 'success' : 'default'}>
                    {trigger.enabled ? 'Enabled' : 'Disabled'}
                  </Badge>
                </div>
                <div className="flex items-center gap-3 mt-1 text-sm text-gray-400">
                  <span className="capitalize">{trigger.trigger_type.replace('_', ' ')}</span>
                  {trigger.threshold && (
                    <>
                      <span>•</span>
                      <span>Threshold: {trigger.threshold} plays</span>
                    </>
                  )}
                  {trigger.cron_expression && (
                    <>
                      <span>•</span>
                      <span>Cron: {trigger.cron_expression}</span>
                    </>
                  )}
                  {trigger.playlist_name && (
                    <>
                      <span>•</span>
                      <span>→ {trigger.playlist_name}</span>
                    </>
                  )}
                </div>
                {trigger.last_run && (
                  <p className="text-xs text-gray-500 mt-1">
                    Last run: {formatDate(trigger.last_run)}
                  </p>
                )}
                {trigger.navidrome_playlist_id && (
                  <p className="text-xs text-green-500/70 mt-0.5">
                    Linked to Navidrome playlist
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant={trigger.enabled ? 'secondary' : 'primary'}
                  onClick={() => handleToggle(trigger.id)}
                >
                  {trigger.enabled ? 'Disable' : 'Enable'}
                </Button>
                <Button variant="danger" onClick={() => handleDelete(trigger.id)}>
                  Delete
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
