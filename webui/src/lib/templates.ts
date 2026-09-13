/**
 * Maintenance task templates.
 *
 * A template is a convenience for re-entering parameters the operator already
 * validated: it stores no credentials, and every template is re-validated
 * through the platform preview before anything can be queued.
 */
import type { MaintenanceTaskRequest } from "./api";

const STORAGE_KEY = "market-data-center.task-templates.v1";

export type TaskTemplate = {
  name: string;
  saved_at: string;
  task: MaintenanceTaskRequest;
};

const isTemplate = (value: unknown): value is TaskTemplate => {
  const candidate = value as TaskTemplate;
  return Boolean(candidate && typeof candidate.name === "string" && candidate.task
    && typeof candidate.task === "object");
};

export function loadTemplates(): TaskTemplate[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isTemplate) : [];
  } catch {
    // A console must keep working when storage is unavailable or corrupt.
    return [];
  }
}

function persist(templates: TaskTemplate[]): TaskTemplate[] {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(templates));
  } catch {
    /* storage is a convenience, never a source of truth */
  }
  return templates;
}

export function saveTemplate(name: string, task: MaintenanceTaskRequest): TaskTemplate[] {
  const trimmed = name.trim();
  if (!trimmed) return loadTemplates();
  const templates = loadTemplates().filter(item => item.name !== trimmed);
  templates.unshift({ name: trimmed, saved_at: new Date().toISOString(), task });
  return persist(templates.slice(0, 20));
}

export function deleteTemplate(name: string): TaskTemplate[] {
  return persist(loadTemplates().filter(item => item.name !== name));
}
