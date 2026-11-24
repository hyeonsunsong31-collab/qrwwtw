import { useState } from 'react';

export default function App() {
  const [count, setCount] = useState(0);
  return (
    <main>
      <h1>{{TITLE}}</h1>
      <p id="count">Count: {count}</p>
      <button id="inc" type="button" onClick={() => setCount(count + 1)}>Add one</button>
    </main>
  );
}
