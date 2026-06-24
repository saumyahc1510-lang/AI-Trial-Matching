import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  BellOff, BellRing, CheckCheck, Inbox, Sparkles, Telescope,
  AlertCircle, FlaskConical,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { SkeletonRows } from '@/components/ui/Skeleton.jsx';
import {
  listNotifications, markAllRead, markRead,
} from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

const TYPE_META = {
  new_match:        { Icon: Sparkles,     tone: 'brand'   },
  match_resolved:   { Icon: Telescope,    tone: 'success' },
  trial_opened:     { Icon: FlaskConical, tone: 'success' },
  trial_closed:     { Icon: FlaskConical, tone: 'ink'     },
  data_request:     { Icon: AlertCircle,  tone: 'warn'    },
  system:           { Icon: BellRing,     tone: 'brand'   },
};

export default function Notifications() {
  const qc = useQueryClient();
  const [unreadOnly, setUnreadOnly] = useState(false);

  const notifsQ = useQuery({
    queryKey: ['notifications', 'list', unreadOnly],
    queryFn: () => listNotifications({ unread_only: unreadOnly, limit: 100 }),
  });

  const readOne = useMutation({
    mutationFn: markRead,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notifications'] });
    },
  });
  const readAll = useMutation({
    mutationFn: markAllRead,
    onSuccess: (data) => {
      toast.success(`Marked ${data?.updated ?? 'all'} as read.`);
      qc.invalidateQueries({ queryKey: ['notifications'] });
    },
  });

  const notifications = notifsQ.data || [];

  return (
    <div>
      <PageHeader
        eyebrow="Inbox"
        title="Notifications"
        description="Match-engine, sync worker, and wearable-resolution events land here."
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setUnreadOnly((u) => !u)}
              className={cn('btn-secondary', unreadOnly && 'border-brand-300 bg-brand-50 text-brand-700')}
            >
              {unreadOnly ? <BellRing className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
              {unreadOnly ? 'Showing unread' : 'Show all'}
            </button>
            <button
              onClick={() => readAll.mutate()}
              className="btn-primary"
              disabled={readAll.isPending}
            >
              <CheckCheck className="h-4 w-4" /> Mark all read
            </button>
          </div>
        }
      />

      {notifsQ.isLoading ? (
        <SkeletonRows rows={6} />
      ) : notifications.length === 0 ? (
        <EmptyState
          icon={Inbox}
          title="Nothing to report"
          description="Run a match to populate the inbox.  New matches, status flips, and resolved uncertainties all surface here."
        />
      ) : (
        <ul className="space-y-2.5">
          <AnimatePresence initial={false}>
            {notifications.map((n, i) => (
              <motion.li
                key={n.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -16 }}
                transition={{ delay: Math.min(i, 10) * 0.03 }}
              >
                <NotificationRow
                  notification={n}
                  onMarkRead={() => readOne.mutate(n.id)}
                />
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </div>
  );
}

const TONE_RING = {
  brand:   'bg-brand-100 text-brand-600',
  success: 'bg-success-100 text-success-600',
  warn:    'bg-warn-100 text-warn-600',
  ink:     'bg-ink-100 text-ink-500',
};

function NotificationRow({ notification, onMarkRead }) {
  const meta = TYPE_META[notification.notification_type] || TYPE_META.system;
  const Icon = meta.Icon;
  return (
    <div className={cn(
      'card flex items-start gap-4 p-4 transition-shadow',
      !notification.read && 'shadow-glow',
    )}>
      <span className={cn(
        'flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl',
        TONE_RING[meta.tone],
      )}>
        <Icon className="h-5 w-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-center gap-2">
            {!notification.read && (
              <span className="inline-block h-2 w-2 rounded-full bg-accent-500" />
            )}
            <span className="font-medium text-ink-800">{notification.title}</span>
          </div>
          <span className="text-xs text-ink-400">
            {formatDistanceToNow(new Date(notification.created_at), { addSuffix: true })}
          </span>
        </div>
        <p className="mt-0.5 text-sm text-ink-600">{notification.message}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wider text-ink-400">
          <span>{notification.notification_type.replace(/_/g, ' ')}</span>
          <span>·</span>
          <span>{notification.channel}</span>
        </div>
      </div>
      {!notification.read && (
        <button
          onClick={onMarkRead}
          className="btn-ghost shrink-0"
          title="Mark as read"
        >
          Mark read
        </button>
      )}
    </div>
  );
}
