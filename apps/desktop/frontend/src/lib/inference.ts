/**
 * Inference routing abstraction - v1-ready.
 *
 * v0: only LocalProvider is active. NetworkProvider and FallbackProvider
 * are stubs that always return isAvailable() === false.
 *
 * v1 plan: NetworkProvider will route to the contributor network for large
 * models that exceed local VRAM. FallbackProvider will offer a degraded
 * experience (smaller local model) if neither is available.
 */

import type { ChatMessage, ChatOptions } from '@/types/chat';
import { localApi } from './localApi';

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

interface InferenceProvider {
  readonly name: string;
  streamChat(messages: ChatMessage[], options: ChatOptions): Promise<Response>;
  isAvailable(): Promise<boolean>;
}

// ---------------------------------------------------------------------------
// Providers
// ---------------------------------------------------------------------------

class LocalProvider implements InferenceProvider {
  readonly name = 'local';

  streamChat(messages: ChatMessage[], options: ChatOptions): Promise<Response> {
    return localApi.streamChat(messages, options);
  }

  async isAvailable(): Promise<boolean> {
    try {
      const health = await localApi.health();
      return health.model_loaded === true;
    } catch {
      return false;
    }
  }
}

class NetworkProvider implements InferenceProvider {
  readonly name = 'network';

  streamChat(_messages: ChatMessage[], _options: ChatOptions): Promise<Response> {
    // TODO: route through the contributor network coordinator
    return Promise.reject(new Error('NetworkProvider not available in v0'));
  }

  isAvailable(): Promise<boolean> {
    // Stubbed for v0 - always returns false
    return Promise.resolve(false);
  }
}

class FallbackProvider implements InferenceProvider {
  readonly name = 'fallback';

  streamChat(_messages: ChatMessage[], _options: ChatOptions): Promise<Response> {
    // TODO: attempt a smaller local model if the primary is OOM
    return Promise.reject(new Error('FallbackProvider not available in v0'));
  }

  isAvailable(): Promise<boolean> {
    // Stubbed for v0 - always returns false
    return Promise.resolve(false);
  }
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

const PROVIDERS: InferenceProvider[] = [
  new LocalProvider(),
  new NetworkProvider(),
  new FallbackProvider(),
];

/**
 * Route an inference request to the first available provider.
 *
 * Providers are tried in priority order:
 *   1. LocalProvider - always preferred; zero latency, zero data egress
 *   2. NetworkProvider - contributor network (v1)
 *   3. FallbackProvider - degraded local fallback (v1)
 *
 * @throws if no provider is available
 */
export async function routeInference(
  messages: ChatMessage[],
  options: ChatOptions,
  _requestedModel: string,
): Promise<{ response: Response; provider: string }> {
  for (const provider of PROVIDERS) {
    if (await provider.isAvailable()) {
      const response = await provider.streamChat(messages, options);
      return { response, provider: provider.name };
    }
  }

  throw new Error(
    'No inference provider available. Ensure a model is loaded in the local sidecar.',
  );
}
