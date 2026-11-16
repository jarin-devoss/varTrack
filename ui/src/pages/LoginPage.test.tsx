// @vitest-environment jsdom
/**
 * The sign-in page draws a way to sign in only once the orchestrator has said which one applies. Until then it says it is
 * checking; when it cannot be asked it says so. The token form is the answer "sign-in is off", and is shown for that answer only:
 * drawn before the answer, it told a person on a signed-in deployment that there was nothing to sign in with.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiProvider, type VarTrackApi } from '../api';
import type { AuthSession } from '../api/types';
import { LoginPage } from './LoginPage';

afterEach(cleanup);

const SIGN_IN_ON: AuthSession = {
  enabled: true,
  authenticated: false,
  platforms: [{ platform: 'gitea', provider: 'gitea' } as AuthSession['platforms'][number]],
};
const SIGN_IN_OFF: AuthSession = { enabled: false, authenticated: false, platforms: [] };

function page(getSession: () => Promise<AuthSession>) {
  // Each page gets its own API: the query cache is kept per API, so one test's answer is never another's.
  const api = { getSession, loginUrl: (platform: string) => `/login-with/${platform}` } as unknown as VarTrackApi;
  return render(
    <MemoryRouter initialEntries={['/login?return_url=%2Frules']}>
      <ApiProvider api={api}>
        <LoginPage />
      </ApiProvider>
    </MemoryRouter>,
  );
}

const tokenField = () => screen.queryByPlaceholderText('Paste the token');
const giteaLink = () => screen.queryByRole('link', { name: /sign in with gitea/i });

describe('the sign-in page', () => {
  it('says it is checking, and draws neither way to sign in, until the orchestrator has answered', async () => {
    page(() => new Promise<AuthSession>(() => {}));

    expect(screen.getByText('Checking how to sign in…')).toBeTruthy();
    expect(tokenField()).toBeNull();
    expect(giteaLink()).toBeNull();
  });

  it('offers the Git platform when sign-in is on, and never the token form as the way in', async () => {
    page(() => Promise.resolve(SIGN_IN_ON));

    const link = await screen.findByRole('link', { name: /sign in with gitea/i });
    expect(link.getAttribute('href')).toBe('/login-with/gitea');
    expect(screen.queryByText(/Continue without a token/)).toBeNull();
  });

  it('offers the token form when the orchestrator says sign-in is off', async () => {
    page(() => Promise.resolve(SIGN_IN_OFF));

    expect(await screen.findByPlaceholderText('Paste the token')).toBeTruthy();
    expect(giteaLink()).toBeNull();
  });

  it('says it could not ask, with a way to try again, and does not claim sign-in is off', async () => {
    const getSession = vi.fn<() => Promise<AuthSession>>().mockRejectedValueOnce(new Error('down')).mockResolvedValueOnce(SIGN_IN_ON);
    page(getSession);

    expect(await screen.findByText(/can't reach the orchestrator/i)).toBeTruthy();
    expect(tokenField()).toBeNull();
    expect(giteaLink()).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /try again/i }));

    expect(await screen.findByRole('link', { name: /sign in with gitea/i })).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(/can't reach the orchestrator/i)).toBeNull());
    expect(getSession).toHaveBeenCalledTimes(2);
  });
});
