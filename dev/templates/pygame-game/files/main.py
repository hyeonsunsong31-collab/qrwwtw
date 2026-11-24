"""{{TITLE}} -- desktop game (pygame)."""
import random
import sys

import pygame

W, H = 800, 500


def main():
    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("{{TITLE}}")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont(None, 32)
    player = pygame.Rect(W // 2, H // 2, 28, 28)
    coin = pygame.Rect(random.randint(20, W - 40), random.randint(20, H - 40), 18, 18)
    score = 0
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return 0
        keys = pygame.key.get_pressed()
        speed = 5
        player.x += (keys[pygame.K_RIGHT] or keys[pygame.K_d]) * speed - (keys[pygame.K_LEFT] or keys[pygame.K_a]) * speed
        player.y += (keys[pygame.K_DOWN] or keys[pygame.K_s]) * speed - (keys[pygame.K_UP] or keys[pygame.K_w]) * speed
        player.clamp_ip(screen.get_rect())
        if player.colliderect(coin):
            score += 1
            coin.topleft = (random.randint(20, W - 40), random.randint(20, H - 40))
        screen.fill((13, 20, 36))
        pygame.draw.ellipse(screen, (250, 204, 21), coin)
        pygame.draw.rect(screen, (56, 189, 248), player)
        screen.blit(font.render(f"Score: {score}", True, (220, 230, 255)), (12, 10))
        pygame.display.flip()
        clock.tick(60)


if __name__ == "__main__":
    sys.exit(main())
