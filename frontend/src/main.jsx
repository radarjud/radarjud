import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { AuthProvider } from './AuthContext.jsx';
import './styles.css';
import './Auth.css';

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error('RadarJud: erro ao renderizar a tela', error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div style={{ padding: 32, fontFamily: 'Inter,Arial,sans-serif', background: '#f8f7f5', minHeight: '100vh', color: '#142335' }}>
        <h1>Não foi possível exibir esta página</h1>
        <p>Ocorreu um erro na interface. Tente recarregar a página.</p>
        {import.meta.env.DEV && (
          <pre style={{ whiteSpace: 'pre-wrap', background: '#fff', padding: 20, borderRadius: 8, border: '1px solid #ddd' }}>
            {String(this.state.error.message || this.state.error)}
          </pre>
        )}
        <button
          style={{ padding: 12, background: '#245a81', color: 'white', border: 0, borderRadius: 8, cursor: 'pointer' }}
          onClick={() => window.location.reload()}
        >
          Recarregar página
        </button>
      </div>
    );
  }
}

createRoot(document.getElementById('root')).render(
  <ErrorBoundary>
    <AuthProvider>
      <App />
    </AuthProvider>
  </ErrorBoundary>
);