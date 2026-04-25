use std::env;

fn main() {
    let args: Vec<String> = env::args().collect();
    let who = if args.len() > 1 { &args[1] } else { "world" };
    println!("Hello, {}! (from Rust)", who);
}
