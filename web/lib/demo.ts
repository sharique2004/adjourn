/**
 * There is no fabricated fallback data. When the cloud mirror cannot be read —
 * missing credentials, connection failure, timeout — every surface renders an
 * honest empty state and the footer says "mirror unreachable". This module
 * only carries the Dataset shape shared by the cloud reader and the pages.
 */

import type { ExecutionRecord, MeetingRecord, StatementRecord } from './types';

export interface Dataset {
  meetings: MeetingRecord[];
  executions: ExecutionRecord[];
  statements: StatementRecord[];
}
