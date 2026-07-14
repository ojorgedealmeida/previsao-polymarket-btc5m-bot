import "./styles.css";

export const metadata = {
  title: "Bot BTC 5 min",
  robots: "noindex,nofollow"
};

export default function RootLayout({ children }) {
  return (
    <html lang="pt-BR">
      <body>{children}</body>
    </html>
  );
}
