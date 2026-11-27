/**
 * What: Button on the Base UI primitive (shadcn base-nova), restyled to the DESIGN.md variants.
 * Use when: any clickable action in the app.
 * Inputs: Base UI Button props plus `variant` (primary, ink, quiet, ghost) and `size` (default, sm, icon).
 * Output: a <button> element.
 * Side effects: none.
 * Role: the one button vocabulary (DESIGN.md s5: ember Primary for Confirm only, Ink, Quiet).
 */
import { Button as ButtonPrimitive } from "@base-ui/react/button";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * What: class recipe for the Button variants and sizes.
 * Use when: Button renders, or a link must look like a button.
 * Inputs: { variant, size, className }.
 * Output: a class string.
 * Side effects: none.
 * Role: the one button style source.
 */
const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-2 rounded-md border text-label whitespace-nowrap outline-none select-none transition-colors duration-150 ease-out-expo focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        primary: "border-transparent bg-primary text-primary-foreground hover:bg-primary/90",
        ink: "border-transparent bg-foreground text-background hover:bg-foreground/90",
        quiet: "border-border bg-raised text-foreground hover:bg-margin",
        ghost: "border-transparent bg-transparent text-muted-foreground hover:bg-margin hover:text-foreground",
      },
      size: {
        default: "h-9 px-4",
        sm: "h-8 px-3",
        icon: "size-9",
      },
    },
    defaultVariants: {
      variant: "quiet",
      size: "default",
    },
  },
);

/**
 * What: the Button component.
 * Use when: rendering an action; pick the variant by meaning, not looks.
 * Inputs: ButtonPrimitive.Props, variant, size, className.
 * Output: JSX button.
 * Side effects: none.
 * Role: shared UI primitive.
 */
function Button({
  className,
  variant,
  size,
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return <ButtonPrimitive data-slot="button" className={cn(buttonVariants({ variant, size, className }))} {...props} />;
}

export { Button, buttonVariants };
