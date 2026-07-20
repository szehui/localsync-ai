import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react';
import { api } from './api';
import type { UserResponse } from './types';

interface AuthState {
  user: UserResponse | null;
  loading: boolean;
  authenticated: boolean;
}

interface AuthContextType extends AuthState {
  login: (token: string) => void;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    loading: true,
    authenticated: false,
  });

  const tryRestore = useCallback(async () => {
    const token = localStorage.getItem('access_token');
    if (!token) {
      setState({ user: null, loading: false, authenticated: false });
      return;
    }
    try {
      const user = await api.me();
      setState({ user, loading: false, authenticated: true });
    } catch {
      localStorage.removeItem('access_token');
      setState({ user: null, loading: false, authenticated: false });
    }
  }, []);

  useEffect(() => { tryRestore(); }, [tryRestore]);

  const login = (token: string) => {
    localStorage.setItem('access_token', token);
    // fetch user info
    api.me().then((user) => {
      setState({ user, loading: false, authenticated: true });
    }).catch(() => {
      // token was just set, this should work — but if not, log them out
      localStorage.removeItem('access_token');
      setState({ user: null, loading: false, authenticated: false });
    });
  };

  const logout = async () => {
    try { await api.logout(); } catch { /* ignore */ }
    localStorage.removeItem('access_token');
    setState({ user: null, loading: false, authenticated: false });
  };

  return (
    <AuthContext.Provider value={{ ...state, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}