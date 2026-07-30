// `plotly.js-dist-min` is the same public API as `plotly.js`, pre-built and
// minified, but ships no type declarations of its own -- reuse @types/plotly.js.
declare module 'plotly.js-dist-min' {
  export * from 'plotly.js';
  import Plotly from 'plotly.js';
  export default Plotly;
}
