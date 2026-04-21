/**
 * Re-export from views so component-level imports work alongside
 * the route-level view import. Consuming code may prefer either path.
 *
 * TODO (guide §4.5): This file will grow into a self-contained feature
 * module once streaming, MessageList, Composer, and ModelSelector are wired.
 */
export { ChatView } from '@/views/ChatView';
