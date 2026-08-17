export interface SystemInfo {
  name: string;
  version: string;
  environment: string;
}

const configuredApiBaseUrl: unknown = import.meta.env.VITE_API_BASE_URL;
const apiBaseUrl =
  typeof configuredApiBaseUrl === "string" ? configuredApiBaseUrl : "/api/v1";

function isSystemInfo(value: unknown): value is SystemInfo {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.name === "string" &&
    typeof candidate.version === "string" &&
    typeof candidate.environment === "string"
  );
}

export async function getSystemInfo(signal?: AbortSignal): Promise<SystemInfo> {
  const response = await fetch(`${apiBaseUrl}/system/info`, { signal });
  if (!response.ok) {
    throw new Error(`API returned ${response.status}`);
  }
  const payload: unknown = await response.json();
  if (!isSystemInfo(payload)) {
    throw new Error("API returned an invalid system info payload");
  }
  return payload;
}
