import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { AuthProvider } from './context/AuthContext.jsx';
import './styles.css';
import './auth.css';

class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) { console.error('RadarJud: erro ao renderizar a tela', error, info); }
  render() {
    if (this.state.error) {
      return React.createElement(
        'div',
        {
          style: {
            padding: 32,
            fontFamily: 'Inter,Arial,sans-serif',
            background: '#f8f7f5',
            minHeight: '100vh',
            color: '#142335',
          },
        },
        React.createElement('h1', null, 'Não foi possível exibir esta página'),
        React.createElement(
          'p',
          null,
          'Ocorreu um erro na interface. Nenhum arquivo do backend foi alterado.'
        ),
        React.createElement(
          'pre',
          {
            style: {
              whiteSpace: 'pre-wrap',
              background: '#fff',
              padding: 20,
              borderRadius: 8,
              border: '1px solid #ddd',
            },
          },
          String(this.state.error.message || this.state.error)
        ),
        React.createElement(
          'button',
          {
            style: {
              padding: 12,
              background: '#245a81',
              color: 'white',
              border: 0,
              borderRadius: 8,
              cursor: 'pointer',
            },
            onClick: () => window.location.reload(),
          },
          'Recarregar página'
        )
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById('root')).render(<ErrorBoundary><AuthProvider><App /></AuthProvider></ErrorBoundary>);